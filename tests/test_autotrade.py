from trading_agent.agent import TradingAgent
from trading_agent.autopilot import AutoPilot
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.llm_agent import _tool_defs
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def make_agent(tmp_path, **overrides):
    defaults = dict(max_order_krw=10_000_000, max_position_pct=1.0, base_equity_krw=10_000_000)
    defaults.update(overrides)
    settings = Settings(kis_env="mock", data_dir=tmp_path, **defaults)
    return TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)


def test_auto_execute_simulated_in_mock(tmp_path):
    agent = make_agent(tmp_path)
    price = agent.analyze("005930")["quote"]["price"]
    result = agent.auto_execute("005930", "buy", 1, price)
    assert result["status"] == "executed"
    assert result["dry_run"] is True  # mock always simulates
    positions = agent.balance("KR")["agent_portfolio"]["positions"]
    assert any(p["symbol"] == "005930" for p in positions)


def test_auto_execute_respects_risk_gate(tmp_path):
    agent = make_agent(tmp_path, max_order_krw=1)  # any order exceeds the cap
    price = agent.analyze("005930")["quote"]["price"]
    result = agent.auto_execute("005930", "buy", 1, price)
    assert result["status"] == "blocked"
    assert result["blocks"]


def test_place_order_tool_only_when_auto_trade():
    assert "place_order" not in {t["name"] for t in _tool_defs(auto_trade=False)}
    assert "place_order" in {t["name"] for t in _tool_defs(auto_trade=True)}


def test_autopilot_rule_decisions():
    candidates = [
        {"symbol": "A", "market": "KR", "held": False, "action": "BUY_CANDIDATE",
         "suggested_quantity": 3, "qty": 0, "unrealized_pct": 0, "price": 100},
        {"symbol": "B", "market": "KR", "held": True, "action": "WATCH",
         "suggested_quantity": 0, "qty": 5, "unrealized_pct": -6, "price": 90},   # stop
        {"symbol": "C", "market": "KR", "held": True, "action": "WATCH",
         "suggested_quantity": 0, "qty": 2, "unrealized_pct": 12, "price": 110},  # target
        {"symbol": "D", "market": "KR", "held": True, "action": "WATCH",
         "suggested_quantity": 0, "qty": 1, "unrealized_pct": 3, "price": 103},   # hold
    ]
    decisions = {d["symbol"]: d["side"] for d in AutoPilot._rule_decisions(candidates)}
    assert decisions == {"A": "buy", "B": "sell", "C": "sell"}


def test_autopilot_cycle_runs(tmp_path):
    agent = make_agent(tmp_path)
    pilot = AutoPilot(lambda: agent, lambda: agent.settings, agent.store)
    executed = pilot.cycle()
    assert isinstance(executed, list)
    for entry in executed:
        assert entry["status"] in {"executed", "blocked"}
