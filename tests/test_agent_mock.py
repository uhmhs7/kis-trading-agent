from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def test_mock_agent_analyzes_symbol(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000)
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    result = agent.analyze("005930")
    assert result["symbol"] == "005930"
    assert result["quote"]["price"] > 0
    assert result["action"]["label"] in {"BUY_CANDIDATE", "WATCH", "AVOID_OR_WAIT"}


def test_order_preview_creates_approval(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000)
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    analysis = agent.analyze("005930")
    preview = agent.preview_order(
        "005930",
        "buy",
        1,
        analysis["quote"]["price"],
        dry_run=True,
    )
    assert preview["approval"]["status"] == "pending"


def test_analysis_is_deterministic_and_has_breakout_baseline(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000)
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    first = agent.analyze("005930")
    second = agent.analyze("005930")
    assert isinstance(first["score"], int)
    assert first["score"] == second["score"]
    # breakout signal uses the prior 20-day window (excludes today), so today's high
    # never trivially satisfies the breakout.
    assert "prior_high20" in first["metrics"]
    assert first["metrics"]["prior_high20"] < max(b["high"] for b in first["recent_bars"]) + 1


def test_chat_falls_back_to_keyword_router_without_llm(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000)
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    assert agent.llm.available is False
    assert agent.chat("잔고")["kind"] == "balance"
    assert agent.chat("005930 분석")["kind"] == "analysis"
    assert agent.chat("아무말")["kind"] == "fallback"

