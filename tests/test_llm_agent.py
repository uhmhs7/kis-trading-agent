"""Exercise the LLM tool-use loop with a fake Anthropic client (no network/key)."""
from types import SimpleNamespace

from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.llm_agent import LLMAgent
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def _block(**kw):
    return SimpleNamespace(**kw)


class FakeMessages:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


class FakeClient:
    def __init__(self, scripted):
        self.messages = FakeMessages(scripted)


def _make(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000, anthropic_api_key="test-key")
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    return agent, settings


def test_llm_runs_tool_then_answers(tmp_path):
    agent, settings = _make(tmp_path)
    scripted = [
        # Turn 1: model asks for the analyze_stock tool
        _block(
            stop_reason="tool_use",
            content=[
                _block(type="tool_use", name="analyze_stock", input={"symbol": "005930"}, id="t1"),
            ],
        ),
        # Turn 2: model writes the final answer
        _block(
            stop_reason="end_turn",
            content=[_block(type="text", text="삼성전자 분석을 마쳤습니다.")],
        ),
    ]
    llm = LLMAgent(agent, settings)
    llm._client = FakeClient(scripted)

    result = llm.chat("삼성전자 분석해줘")

    assert result["kind"] == "agent"
    assert result["message"] == "삼성전자 분석을 마쳤습니다."
    assert result["tool_calls"] == ["analyze_stock"]
    assert any(a["type"] == "analysis" for a in result["artifacts"])
    # The second create() call must carry the tool_result back to the model.
    second_messages = llm._client.messages.calls[1]["messages"]
    assert second_messages[-1]["content"][0]["type"] == "tool_result"


def test_llm_unavailable_without_key(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path)  # no anthropic key
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    assert LLMAgent(agent, settings).available is False
