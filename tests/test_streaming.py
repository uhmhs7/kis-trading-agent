"""Exercise the streaming tool-use loop with a fake Anthropic streaming client."""
from types import SimpleNamespace

from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.llm_agent import LLMAgent
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def _b(**kw):
    return SimpleNamespace(**kw)


class FakeStream:
    def __init__(self, deltas, final):
        self._deltas = deltas
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def text_stream(self):
        return iter(self._deltas)

    def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, streams):
        self.streams = list(streams)

    def stream(self, **kwargs):
        return self.streams.pop(0)


class FakeClient:
    def __init__(self, streams):
        self.messages = FakeMessages(streams)


def _make(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000, anthropic_api_key="k")
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    return agent, settings


def test_chat_stream_emits_tool_then_tokens(tmp_path):
    agent, settings = _make(tmp_path)
    final_tool = _b(
        stop_reason="tool_use",
        content=[_b(type="tool_use", name="analyze_stock", input={"symbol": "005930"}, id="t1")],
    )
    final_text = _b(stop_reason="end_turn", content=[_b(type="text", text="분석 완료")])
    llm = LLMAgent(agent, settings)
    llm._client = FakeClient([FakeStream([], final_tool), FakeStream(["분석 ", "완료"], final_text)])

    events = list(llm.chat_stream("삼성전자 분석"))
    types = [e["type"] for e in events]
    assert "tool" in types
    assert types.count("delta") == 2
    assert events[-1]["type"] == "done"
    done = events[-1]
    assert done["message"] == "분석 완료"
    assert done["tool_calls"] == ["analyze_stock"]
    assert any(a["type"] == "analysis" for a in done["artifacts"])


def test_chat_stream_session_persists_and_returns_session(tmp_path):
    agent, settings = _make(tmp_path)
    final_text = _b(stop_reason="end_turn", content=[_b(type="text", text="안녕하세요")])
    agent.llm._client = FakeClient([FakeStream(["안녕", "하세요"], final_text)])

    events = list(agent.chat_stream_session("안녕"))
    done = events[-1]
    assert done["type"] == "done"
    assert done["session_id"]
    conv = agent.store.get_conversation(done["session_id"])
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["content"] == "안녕하세요"


def test_chat_stream_session_keyword_fallback_without_llm(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, max_order_krw=100_000)  # no key
    agent = TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)
    events = list(agent.chat_stream_session("005930 분석"))
    done = events[-1]
    assert done["type"] == "done"
    assert any(a["type"] == "analysis" for a in done["artifacts"])
