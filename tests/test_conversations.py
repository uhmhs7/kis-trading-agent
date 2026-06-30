from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def make_agent(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000)
    return TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)


def test_chat_session_persists_multi_turn(tmp_path):
    agent = make_agent(tmp_path)
    first = agent.chat_session("005930 분석")
    session_id = first["session_id"]
    assert session_id
    second = agent.chat_session("잔고", session_id=session_id)
    assert second["session_id"] == session_id

    conv = agent.store.get_conversation(session_id)
    roles = [m["role"] for m in conv["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert conv["messages"][0]["content"] == "005930 분석"


def test_list_conversations(tmp_path):
    agent = make_agent(tmp_path)
    agent.chat_session("005930 분석")
    listed = agent.store.list_conversations()
    assert len(listed) == 1
    assert listed[0]["message_count"] == 2
    assert listed[0]["preview"]


def test_empty_message_returns_session_without_persisting(tmp_path):
    agent = make_agent(tmp_path)
    result = agent.chat_session("   ")
    assert result["kind"] == "empty"
    assert agent.store.get_conversation(result["session_id"]) is None
