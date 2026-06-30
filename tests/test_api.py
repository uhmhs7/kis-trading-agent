import os
import tempfile

# Configure the environment BEFORE importing the app so get_settings() (called at
# import time) picks up the test data dir and a clean mock config.
os.environ["KIS_ENV"] = "mock"
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["KIS_TOKEN_CACHE_DIR"] = tempfile.mkdtemp()
os.environ["KIS_MAX_ORDER_KRW"] = "50000"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DASHBOARD_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from trading_agent.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"ok": True}


def test_status_exposes_flags():
    body = client.get("/api/status").json()
    assert body["environment"] == "mock"
    assert "llm_ready" in body
    assert "auth_required" in body
    assert body["auth_required"] is False


def test_analyze_ok():
    res = client.post("/api/analyze", json={"symbol": "005930"})
    assert res.status_code == 200
    assert res.json()["action"]["label"] in {"BUY_CANDIDATE", "WATCH", "AVOID_OR_WAIT"}


def test_analyze_bad_symbol_returns_400():
    # Neither a 6-digit KR code nor a 1-5 letter US ticker.
    res = client.post("/api/analyze", json={"symbol": "1234567"})
    assert res.status_code == 400
    assert "detail" in res.json()


def test_analyze_us_ticker():
    res = client.post("/api/analyze", json={"symbol": "AAPL"})
    assert res.status_code == 200
    body = res.json()
    assert body["market"] == "US"
    assert body["currency"] == "USD"
    assert body["name"] == "애플"


def test_get_config():
    body = client.get("/api/config").json()
    assert body["available_models"]
    assert "model" in body and "thinking" in body and "thinking_supported" in body


def test_set_config_model_and_thinking():
    res = client.post("/api/config", json={"model": "claude-sonnet-4-6", "thinking": True})
    assert res.status_code == 200
    body = res.json()
    assert body["model"] == "claude-sonnet-4-6"
    assert body["thinking"] is True
    assert body["thinking_supported"] is True


def test_set_config_rejects_unknown_model():
    res = client.post("/api/config", json={"model": "gpt-4"})
    assert res.status_code == 400


def test_config_environment_guardrails():
    body = client.get("/api/config").json()
    assert body["environment"] == "mock"
    # No credentials/token in the test env → only mock is switchable.
    assert body["available_environments"] == ["mock"]


def test_switch_to_paper_without_token_rejected():
    # Any non-mock switch needs a configured dashboard token (defense in depth).
    res = client.post("/api/config", json={"environment": "paper"})
    assert res.status_code == 403


def test_switch_to_prod_without_token_rejected():
    res = client.post("/api/config", json={"environment": "prod"})
    assert res.status_code == 403  # non-mock needs DASHBOARD_TOKEN configured


def test_dry_run_default_persists():
    res = client.post("/api/config", json={"dry_run_default": False})
    assert res.status_code == 200
    assert res.json()["dry_run_default"] is False
    # restore default for other tests
    client.post("/api/config", json={"dry_run_default": True})


def test_switch_to_mock_is_ok():
    res = client.post("/api/config", json={"environment": "mock"})
    assert res.status_code == 200
    assert res.json()["environment"] == "mock"


def test_env_switch_requires_matching_dashboard_token(monkeypatch):
    import trading_agent.main as m
    from dataclasses import replace as dc_replace

    patched = dc_replace(
        m.settings,
        dashboard_token="secret",
        paper_app_key="k",
        paper_app_secret="s",
        paper_account_no="12345678",
        account_product_code="01",
    )
    monkeypatch.setattr(m, "settings", patched)
    monkeypatch.setattr(m, "_candidate_settings", lambda env: dc_replace(patched, kis_env=env))
    monkeypatch.setattr(
        m, "_apply_environment", lambda env: monkeypatch.setattr(m, "settings", dc_replace(patched, kis_env=env))
    )

    assert client.post("/api/config", json={"environment": "paper"}).status_code == 401  # no token
    bad = client.post("/api/config", json={"environment": "paper"}, headers={"X-Dashboard-Token": "nope"})
    assert bad.status_code == 401  # wrong token
    ok = client.post("/api/config", json={"environment": "paper"}, headers={"X-Dashboard-Token": "secret"})
    assert ok.status_code == 200
    assert ok.json()["environment"] == "paper"
    m.store.update_config(environment="mock")  # reset persisted state


def test_max_order_limit_change_applies():
    # An order over the default 50,000 cap is blocked; raising the cap lets it through.
    price = client.post("/api/analyze", json={"symbol": "005930"}).json()["quote"]["price"]
    blocked = client.post(
        "/api/orders/preview",
        json={"symbol": "005930", "side": "buy", "quantity": 1, "limit_price": price, "dry_run": True},
    ).json()["approval"]
    assert blocked["status"] == "blocked"  # 1 share > 50,000

    assert client.post("/api/config", json={"max_order_krw": 300000}).json()["max_order_krw"] == 300000
    ok = client.post(
        "/api/orders/preview",
        json={"symbol": "005930", "side": "buy", "quantity": 1, "limit_price": price, "dry_run": True},
    ).json()["approval"]
    assert ok["status"] == "pending"
    client.post("/api/config", json={"max_order_krw": 50000})  # reset for other tests


def test_live_orders_toggle_requires_token():
    # The real-money backstop is never unlockable without a configured dashboard token.
    res = client.post("/api/config", json={"allow_live_orders": True})
    assert res.status_code == 403


def test_footgun_guard_drops_privileged_overrides_without_token(monkeypatch):
    # Removing the token must revert persisted prod/live-unlock to safe defaults.
    import trading_agent.main as m
    from dataclasses import replace as dc_replace

    base = dc_replace(
        m.settings,
        dashboard_token="",  # token removed
        kis_env="mock",
        paper_app_key="k",
        paper_app_secret="s",
        paper_account_no="12345678",
        account_product_code="01",
    )
    monkeypatch.setattr(m, "settings", base)
    m.store.update_config(environment="paper", allow_live_orders=True, max_order_krw=12345)
    captured = {}
    monkeypatch.setattr(m, "_apply_settings", lambda **ov: captured.update(ov))

    m._apply_persisted_overrides()

    assert "allow_live_orders" not in captured  # privileged: dropped without token
    assert captured.get("kis_env") != "paper"  # privileged: not escalated without token
    assert captured.get("max_order_krw") == 12345  # soft cap: kept
    m.store.update_config(environment="mock", allow_live_orders=False, max_order_krw=50000)


def test_auto_trade_toggle_open():
    res = client.post("/api/config", json={"auto_trade": True})
    assert res.status_code == 200
    assert res.json()["auto_trade"] is True
    client.post("/api/config", json={"auto_trade": False})


def test_auto_pilot_llm_requires_token():
    res = client.post("/api/config", json={"auto_pilot_llm": True})
    assert res.status_code == 403  # autonomous LLM calls need a configured token


def test_auto_pilot_start_stop():
    started = client.post("/api/config", json={"auto_pilot": True})
    assert started.status_code == 200
    assert started.json()["auto_pilot"] is True
    stopped = client.post("/api/config", json={"auto_pilot": False}).json()
    assert stopped["auto_pilot"] is False
    assert stopped["auto_pilot_running"] is False


def test_order_over_limit_is_blocked():
    price = client.post("/api/analyze", json={"symbol": "005930"}).json()["quote"]["price"]
    qty = (50_000 // price) + 5  # guaranteed to exceed the 50,000 KRW cap
    res = client.post(
        "/api/orders/preview",
        json={"symbol": "005930", "side": "buy", "quantity": qty, "limit_price": price, "dry_run": True},
    )
    assert res.status_code == 200
    approval = res.json()["approval"]
    assert approval["status"] == "blocked"
    assert any("한도" in block for block in approval["risk_check"]["blocks"])
