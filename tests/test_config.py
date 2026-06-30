"""Runtime model/thinking config: resolution + adaptive-thinking kwarg."""
from types import SimpleNamespace

from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.llm_agent import LLMAgent
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def _b(**kw):
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


def make(tmp_path, **overrides):
    settings = Settings(kis_env="mock", data_dir=tmp_path, **overrides)
    return TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings), settings


def test_llm_config_default_then_override(tmp_path):
    agent, settings = make(tmp_path, anthropic_model="claude-opus-4-8", anthropic_thinking=False)
    assert agent.llm_config() == {"model": "claude-opus-4-8", "thinking": False}
    agent.store.update_config(model="claude-sonnet-4-6", thinking=True)
    assert agent.llm_config() == {"model": "claude-sonnet-4-6", "thinking": True}


def _run_chat(tmp_path, model, thinking):
    agent, settings = make(tmp_path, anthropic_api_key="k")
    agent.store.update_config(model=model, thinking=thinking)
    llm = LLMAgent(agent, settings)
    final = _b(stop_reason="end_turn", content=[_b(type="text", text="ok")])
    llm._client = FakeClient([final])
    llm.chat("안녕")
    return llm._client.messages.calls[0]


def test_thinking_kwarg_passed_for_capable_model(tmp_path):
    call = _run_chat(tmp_path, "claude-sonnet-4-6", True)
    assert call["thinking"] == {"type": "adaptive"}
    assert call["model"] == "claude-sonnet-4-6"


def test_thinking_skipped_when_off(tmp_path):
    call = _run_chat(tmp_path, "claude-sonnet-4-6", False)
    assert "thinking" not in call


def test_thinking_skipped_for_non_capable_model(tmp_path):
    # Haiku isn't in THINKING_MODELS — thinking must not be sent even if toggled on.
    call = _run_chat(tmp_path, "claude-haiku-4-5", True)
    assert "thinking" not in call
    assert call["model"] == "claude-haiku-4-5"
