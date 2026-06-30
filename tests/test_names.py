from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.names import name_for, prompt_table, symbol_for


def test_name_and_symbol_lookup():
    assert name_for("005930") == "삼성전자"
    assert name_for("999999").startswith("종목")
    assert symbol_for("삼성전자") == "005930"
    assert symbol_for("없는회사") is None


def test_prompt_table_contains_pairs():
    table = prompt_table()
    assert "삼성전자=005930" in table


def test_mock_quote_uses_real_name(tmp_path):
    client = KisClient(Settings(kis_env="mock", data_dir=tmp_path))
    assert client.quote("005930").name == "삼성전자"
