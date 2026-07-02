from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import secrets

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth
from .agent import TradingAgent
from .autopilot import AutoPilot
from .config import get_settings
from .kis_client import KisAPIError, KisClient
from .llm_agent import AVAILABLE_MODELS, MODEL_IDS, THINKING_MODELS
from .risk import RiskManager
from .schemas import (
    AnalyzeRequest,
    ChatRequest,
    ConfigRequest,
    OrderExecuteRequest,
    OrderPreviewRequest,
    ScreenRequest,
)
from .storage import JsonStore

logger = logging.getLogger("trading_agent.api")

ROOT = Path(__file__).resolve().parents[2]

settings = get_settings()
store = JsonStore(settings.data_dir)
client = KisClient(settings)
risk_manager = RiskManager(settings)
agent = TradingAgent(client, risk_manager, store, settings)

ENVIRONMENTS = ["mock", "paper", "prod"]


def _candidate_settings(env: str):
    # The current settings already carries all env-file credentials, so switching
    # env is just an override on top of it.
    return replace(settings, kis_env=env)


def _env_available(env: str) -> bool:
    """Can we switch to this env? mock always; others need credentials+account,
    and a dashboard token configured (the public env switch is privileged)."""
    if env == "mock":
        return True
    cand = _candidate_settings(env)
    return cand.has_api_credentials and cand.has_account and settings.privileged_gate


def _apply_settings(**overrides) -> None:
    """Rebuild the client/risk/agent stack with overridden settings and swap globals."""
    global settings, client, risk_manager, agent
    new_settings = replace(settings, **overrides)
    if not new_settings.is_mock:
        new_settings.validate_runtime()  # raises if credentials missing for non-mock
    client = KisClient(new_settings)
    risk_manager = RiskManager(new_settings)
    agent = TradingAgent(client, risk_manager, store, new_settings)
    settings = new_settings


def _apply_environment(env: str) -> None:
    _apply_settings(kis_env=env)


def _apply_persisted_overrides() -> None:
    """On startup, re-apply persisted runtime overrides safely.

    Soft caps (per-order limits) always apply. PRIVILEGED overrides (non-mock env,
    live-order unlock) apply ONLY when a dashboard token is configured — so removing
    the token reverts to the safe env-file defaults (no stale prod/unlock left open).
    De-escalation to mock is always honored.
    """
    cfg = store.read_config()
    overrides = {}
    if cfg.get("max_order_krw") is not None:
        overrides["max_order_krw"] = int(cfg["max_order_krw"])
    if cfg.get("max_order_usd") is not None:
        overrides["max_order_usd"] = float(cfg["max_order_usd"])
    token_configured = bool(settings.dashboard_token)
    if token_configured and cfg.get("allow_live_orders") is not None:
        overrides["allow_live_orders"] = bool(cfg["allow_live_orders"])
    env = cfg.get("environment")
    if env and env != settings.kis_env:
        if env == "mock":
            overrides["kis_env"] = "mock"
        elif token_configured:
            cand = replace(settings, kis_env=env, **overrides)
            if cand.has_api_credentials and cand.has_account:
                overrides["kis_env"] = env
    if overrides:
        try:
            _apply_settings(**overrides)
        except Exception:  # pragma: no cover - missing creds on restart
            logging.getLogger("trading_agent.api").warning("persisted overrides unavailable; using env defaults")


_apply_persisted_overrides()

# Background auto-trading scheduler (lambdas read the live globals, which may be
# swapped by an env change).
autopilot = AutoPilot(lambda: agent, lambda: settings, store)
# Auto-resume ONLY in mock. In a real-account env the operator must re-enable it
# each session (prevents a persisted toggle from auto-trading a real account on restart).
if store.read_config().get("auto_pilot") and settings.is_mock:
    autopilot.start()

app = FastAPI(title="KIS Trading Agent", version="0.1.0")
# Signed-cookie sessions (no server-side store — safe on ephemeral hosts). Set
# SESSION_SECRET in prod so logins survive restarts; a random key logs users out
# on each restart otherwise.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or secrets.token_urlsafe(32),
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def current_user(request: Request):
    return request.session.get("user_email")


def _redirect_uri(request: Request) -> str:
    if settings.oauth_redirect_uri:
        return settings.oauth_redirect_uri
    return str(request.base_url).rstrip("/") + "/auth/callback"


def is_privileged(request: Request, x_dashboard_token: str = "") -> bool:
    """Privileged = logged-in allowlisted Google user, OR the legacy dashboard token."""
    email = current_user(request)
    if email and auth.email_allowed(settings, email):
        return True
    if settings.dashboard_token and x_dashboard_token == settings.dashboard_token:
        return True
    return False


def require_dashboard_token(request: Request, x_dashboard_token: str = Header(default="")) -> None:
    """Gate order/balance routes when a privileged gate (Google login or token) is set.

    No-op for the open mock demo (nothing configured).
    """
    if settings.privileged_gate and not is_privileged(request, x_dashboard_token):
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")


@app.get("/auth/login")
def auth_login(request: Request):
    if not settings.has_google_oauth:
        raise HTTPException(status_code=400, detail="Google 로그인이 구성되어 있지 않습니다.")
    state = auth.new_state()
    request.session["oauth_state"] = state
    return RedirectResponse(auth.build_auth_url(settings, _redirect_uri(request), state))


@app.get("/auth/callback")
def auth_callback(request: Request, code: str = "", state: str = ""):
    if not settings.has_google_oauth:
        raise HTTPException(status_code=400, detail="Google 로그인이 구성되어 있지 않습니다.")
    if not code or not state or state != request.session.get("oauth_state"):
        raise HTTPException(status_code=400, detail="잘못된 로그인 요청입니다.")
    info = auth.exchange_code(settings, code, _redirect_uri(request))
    email = (info or {}).get("email")
    if not email:
        raise HTTPException(status_code=400, detail="구글 인증에 실패했습니다.")
    request.session.pop("oauth_state", None)
    if not auth.email_allowed(settings, email):
        request.session.clear()
        return HTMLResponse(
            "<div style='font-family:sans-serif;padding:48px;text-align:center'>"
            "<h2>접근 권한이 없는 계정입니다</h2>"
            f"<p style='color:#657182'>{email}</p>"
            "<p><a href='/'>← 돌아가기</a></p></div>",
            status_code=403,
        )
    request.session["user_email"] = email
    return RedirectResponse("/")


@app.get("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/api/me")
def api_me(request: Request) -> dict:
    email = current_user(request)
    return {
        "email": email,
        "authorized": is_privileged(request),
        "oauth_enabled": settings.has_google_oauth,
        "auth_required": settings.privileged_gate,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((ROOT / "templates" / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/status")
def status() -> dict:
    cfg = agent.llm_config()
    return {
        "environment": settings.kis_env,
        "api_ready": settings.has_api_credentials,
        "account_ready": settings.has_account,
        "llm_ready": settings.has_llm,
        "llm_model": cfg["model"] if settings.has_llm else None,
        "llm_thinking": cfg["thinking"] and cfg["model"] in THINKING_MODELS,
        "auth_required": settings.privileged_gate,
        "live_orders_enabled": settings.allow_live_orders,
        "max_order_krw": settings.max_order_krw,
        "max_order_usd": settings.max_order_usd,
        "daily_loss_limit_krw": settings.daily_loss_limit_krw,
        "markets": ["KR", "US"],
        "default_watchlist": list(settings.default_watchlist),
        "default_watchlist_us": ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"],
    }


@app.post("/api/analyze")
def analyze(request: AnalyzeRequest) -> dict:
    return _safe_call("analyze", lambda: agent.analyze(request.symbol, request.market))


@app.post("/api/screen")
def screen(request: ScreenRequest) -> dict:
    return _safe_call("screen", lambda: agent.screen(request.symbols, request.market))


@app.get("/api/prices")
def prices(symbol: str, market: str = "KR", days: int = 400) -> dict:
    """Daily bars for the chart, on demand (public — the chart is part of the demo)."""
    return _safe_call("prices", lambda: agent.price_history(symbol, market, days))


@app.post("/api/chat")
def chat(request: ChatRequest) -> dict:
    return _safe_call("chat", lambda: agent.chat_session(request.message, request.session_id))


@app.post("/api/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    def event_source():
        try:
            for event in agent.chat_stream_session(request.message, request.session_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception:  # never leak a traceback into the stream
            logger.exception("chat_stream error")
            store.append_log("error", {"op": "chat_stream"})
            payload = {"type": "error", "message": "요청 처리 중 오류가 발생했습니다."}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _config_payload() -> dict:
    cfg = agent.llm_config()
    return {
        "model": cfg["model"],
        "thinking": cfg["thinking"],
        "thinking_supported": cfg["model"] in THINKING_MODELS,
        "available_models": AVAILABLE_MODELS,
        "llm_ready": settings.has_llm,
        "auth_required": settings.privileged_gate,
        "environment": settings.kis_env,
        "environments": ENVIRONMENTS,
        "available_environments": [e for e in ENVIRONMENTS if _env_available(e)],
        "live_orders_enabled": settings.allow_live_orders,
        "max_order_krw": settings.max_order_krw,
        "max_order_usd": settings.max_order_usd,
        "dry_run_default": bool(store.read_config().get("dry_run_default", True)),
        **agent.auto_config(),
        "auto_pilot_running": autopilot.running,
    }


@app.get("/api/config")
def get_config() -> dict:
    return _config_payload()


@app.post("/api/config")
def set_config(request: ConfigRequest, http_request: Request, x_dashboard_token: str = Header(default="")) -> dict:
    token_ok = is_privileged(http_request, x_dashboard_token)

    # --- Soft settings: no token, no stack rebuild ---
    soft = {}
    if request.model is not None:
        if request.model not in MODEL_IDS:
            raise HTTPException(status_code=400, detail="지원하지 않는 모델입니다.")
        soft["model"] = request.model
    if request.thinking is not None:
        soft["thinking"] = bool(request.thinking)
    if request.dry_run_default is not None:
        soft["dry_run_default"] = bool(request.dry_run_default)
    if request.auto_trade is not None:
        if not settings.is_mock:  # touching a real account → operator token required
            if not settings.privileged_gate:
                raise HTTPException(status_code=403, detail="실계좌 환경의 자동 체결은 DASHBOARD_TOKEN 설정 시에만 허용됩니다.")
            if not token_ok:
                raise HTTPException(status_code=401, detail="자동 체결 변경에는 유효한 대시보드 토큰이 필요합니다.")
        soft["auto_trade"] = bool(request.auto_trade)
    if request.auto_pilot_interval is not None:
        soft["auto_pilot_interval"] = int(request.auto_pilot_interval)
    # LLM autopilot makes autonomous paid calls → always requires the token.
    if request.auto_pilot_llm is not None:
        if not settings.privileged_gate:
            raise HTTPException(status_code=403, detail="LLM 자율매매는 DASHBOARD_TOKEN 설정 시에만 허용됩니다.")
        if not token_ok:
            raise HTTPException(status_code=401, detail="LLM 자율매매 변경에는 유효한 대시보드 토큰이 필요합니다.")
        soft["auto_pilot_llm"] = bool(request.auto_pilot_llm)
    if soft:
        store.update_config(**soft)
        store.append_log("config", soft)

    # Background scheduler on/off. Open in mock (no real account); in paper/prod it
    # drives autonomous real-account activity, so it needs the operator token.
    if request.auto_pilot is not None:
        if request.auto_pilot and not settings.is_mock:
            if not settings.privileged_gate:
                raise HTTPException(status_code=403, detail="실계좌 환경의 자율 매매는 DASHBOARD_TOKEN 설정 시에만 허용됩니다.")
            if not token_ok:
                raise HTTPException(status_code=401, detail="자율 매매 변경에는 유효한 대시보드 토큰이 필요합니다.")
        store.update_config(auto_pilot=bool(request.auto_pilot))
        if request.auto_pilot:
            autopilot.start()
        else:
            autopilot.stop()

    # --- Stack settings (rebuild the client/risk/agent) ---
    stack = {}
    if request.max_order_krw is not None:
        stack["max_order_krw"] = int(request.max_order_krw)
    if request.max_order_usd is not None:
        stack["max_order_usd"] = float(request.max_order_usd)
    # Per-order limits: require the token only when one is configured (harmless cap;
    # open in the mock demo so the limit can be tuned).
    if stack and settings.privileged_gate and not token_ok:
        raise HTTPException(status_code=401, detail="설정 변경에는 유효한 대시보드 토큰이 필요합니다.")

    # Live-order lock: the final real-money backstop — ALWAYS requires a token
    # (never unlockable in the open demo).
    if request.allow_live_orders is not None:
        if not settings.privileged_gate:
            raise HTTPException(status_code=403, detail="실주문 잠금 해제는 DASHBOARD_TOKEN 설정 시에만 허용됩니다.")
        if not token_ok:
            raise HTTPException(status_code=401, detail="실주문 잠금 변경에는 유효한 대시보드 토큰이 필요합니다.")
        stack["allow_live_orders"] = bool(request.allow_live_orders)

    # Environment switch — privileged: non-mock needs credentials/account, and any
    # non-mock switch (or any switch once a token is configured) needs the token.
    if request.environment is not None and request.environment != settings.kis_env:
        env = request.environment
        if env != "mock" or settings.privileged_gate:
            if not settings.privileged_gate:
                raise HTTPException(status_code=403, detail="환경 전환은 DASHBOARD_TOKEN 설정 시에만 허용됩니다.")
            if not token_ok:
                raise HTTPException(status_code=401, detail="환경 전환에는 유효한 대시보드 토큰이 필요합니다.")
        if env != "mock":
            cand = _candidate_settings(env)
            if not (cand.has_api_credentials and cand.has_account):
                raise HTTPException(status_code=400, detail=f"{env} 환경의 자격증명/계좌가 설정되어 있지 않습니다.")
        try:
            _apply_environment(env)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.update_config(environment=env)
        store.append_log("config", {"environment": env})

    if stack:
        store.update_config(**stack)
        _apply_settings(**stack)
        store.append_log("config", stack)
    return _config_payload()


@app.get("/api/conversations")
def conversations() -> dict:
    return {"conversations": store.list_conversations()}


@app.get("/api/conversations/{session_id}")
def conversation(session_id: str) -> dict:
    conv = store.get_conversation(session_id)
    if not conv:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    return conv


@app.get("/api/balance", dependencies=[Depends(require_dashboard_token)])
def balance(market: str = "KR") -> dict:
    return _safe_call("balance", lambda: agent.balance(market))


@app.post("/api/orders/preview", dependencies=[Depends(require_dashboard_token)])
def order_preview(request: OrderPreviewRequest) -> dict:
    return _safe_call(
        "order_preview",
        lambda: agent.preview_order(
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            limit_price=request.limit_price,
            dry_run=request.dry_run,
            market=request.market,
        ),
    )


@app.post("/api/orders/execute", dependencies=[Depends(require_dashboard_token)])
def order_execute(request: OrderExecuteRequest) -> dict:
    return _safe_call(
        "order_execute",
        lambda: agent.execute_order(request.approval_id, request.confirm_text),
    )


@app.get("/api/logs")
def logs(limit: int = 80) -> dict:
    return {"logs": store.read_logs(limit=limit)}


def _safe_call(op: str, fn):
    try:
        return fn()
    except KisAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except requests.RequestException as exc:
        # Network problem talking to KIS — a clean 502, not a leaked traceback.
        logger.warning("%s: upstream request failed: %s", op, exc)
        store.append_log("error", {"op": op, "type": exc.__class__.__name__})
        raise HTTPException(status_code=502, detail="증권사 API 통신에 실패했습니다.") from exc
    except HTTPException:
        raise
    except Exception as exc:  # catch-all: log server-side, return a sanitized 500
        logger.exception("%s: unexpected error", op)
        store.append_log("error", {"op": op, "type": exc.__class__.__name__, "error": str(exc)})
        raise HTTPException(status_code=500, detail="요청 처리 중 오류가 발생했습니다.") from exc
