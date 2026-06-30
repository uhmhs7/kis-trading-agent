import pytest

from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def make_agent(tmp_path, max_order_krw=100_000):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=max_order_krw)
    return TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)


def test_execute_dry_run_marks_executed(tmp_path):
    agent = make_agent(tmp_path)
    price = agent.analyze("005930")["quote"]["price"]
    preview = agent.preview_order("005930", "buy", 1, price, dry_run=True)
    result = agent.execute_order(preview["approval"]["id"])
    assert result["approval"]["status"] == "executed"
    assert result["broker_response"]["output"]["ODNO"].startswith("DRY-")


def test_double_execute_is_blocked(tmp_path):
    agent = make_agent(tmp_path)
    price = agent.analyze("005930")["quote"]["price"]
    approval_id = agent.preview_order("005930", "buy", 1, price, dry_run=True)["approval"]["id"]
    agent.execute_order(approval_id)
    with pytest.raises(ValueError):
        agent.execute_order(approval_id)


def test_execute_unknown_approval_raises(tmp_path):
    agent = make_agent(tmp_path)
    with pytest.raises(ValueError):
        agent.execute_order("does-not-exist")


def test_blocked_order_cannot_execute(tmp_path):
    agent = make_agent(tmp_path, max_order_krw=1)  # any order exceeds the cap -> blocked
    price = agent.analyze("005930")["quote"]["price"]
    preview = agent.preview_order("005930", "buy", 1, price, dry_run=True)
    assert preview["approval"]["status"] == "blocked"
    with pytest.raises(ValueError):
        agent.execute_order(preview["approval"]["id"])
