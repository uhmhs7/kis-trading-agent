import pytest

from trading_agent import markets
from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def make_agent(tmp_path, **overrides):
    settings = Settings(kis_env="mock", data_dir=tmp_path, **overrides)
    return TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)


def _buy(agent, symbol, qty, price, market=None):
    preview = agent.preview_order(symbol, "buy", qty, price, dry_run=True, market=market)
    if preview["approval"]["status"] != "pending":
        raise AssertionError(preview["approval"]["risk_check"]["blocks"])
    return agent.execute_order(preview["approval"]["id"])


# --- markets module ---------------------------------------------------------


def test_detect_and_normalize():
    assert markets.detect_market("005930") == "KR"
    assert markets.detect_market("AAPL") == "US"
    assert markets.normalize_symbol("aapl") == ("AAPL", "US")
    assert markets.normalize_symbol("005930") == ("005930", "KR")
    with pytest.raises(ValueError):
        markets.normalize_symbol("1234567")  # neither KR code nor US ticker


def test_currency_and_exchange_and_names():
    assert markets.currency_of("US") == "USD"
    assert markets.us_exchanges("AAPL") == ("NAS", "NASD")
    assert markets.us_exchanges("JPM") == ("NYS", "NYSE")
    assert markets.name_for("AAPL", "US") == "애플"
    assert markets.format_money(1234.5, "USD") == "$1,234.50"
    assert markets.format_money(50000, "KRW") == "50,000원"


# --- agent over the US market ----------------------------------------------


def test_analyze_us_ticker(tmp_path):
    report = make_agent(tmp_path).analyze("AAPL")
    assert report["market"] == "US"
    assert report["currency"] == "USD"
    assert report["name"] == "애플"
    assert report["quote"]["price"] > 0
    assert report["risk_plan"]["currency"] == "USD"


def test_us_order_over_usd_limit_is_blocked(tmp_path):
    agent = make_agent(tmp_path)  # default max_order_usd = 50
    preview = agent.preview_order("AAPL", "buy", 10, 100.0, dry_run=True)  # $1,000 notional
    assert preview["approval"]["status"] == "blocked"
    blocks = preview["approval"]["risk_check"]["blocks"]
    assert any("$" in b and "한도" in b for b in blocks)
    assert preview["approval"]["risk_check"]["constraints"]["currency"] == "USD"


def test_us_order_within_limit_is_pending(tmp_path):
    agent = make_agent(tmp_path)
    preview = agent.preview_order("AAPL", "buy", 1, 20.0, dry_run=True)  # $20 notional
    assert preview["approval"]["status"] == "pending"


def test_per_market_portfolios_are_separate(tmp_path):
    agent = make_agent(tmp_path)
    _buy(agent, "005930", 1, 40_000)  # KR, within KRW limits
    _buy(agent, "AAPL", 1, 20.0)  # US, within USD limits

    kr = agent.balance("KR")["agent_portfolio"]
    us = agent.balance("US")["agent_portfolio"]
    assert kr["currency"] == "KRW" and us["currency"] == "USD"
    assert [p["symbol"] for p in kr["positions"]] == ["005930"]
    assert [p["symbol"] for p in us["positions"]] == ["AAPL"]
    assert us["positions"][0]["avg_cost"] == 20.0  # USD keeps decimals


def test_broker_portfolio_handles_dict_and_list_summary(tmp_path):
    agent = make_agent(tmp_path)
    # overseas: summary is a DICT (this used to crash with KeyError: 0)
    us = agent._broker_portfolio(
        {"summary": {"tot_evlu_pfls_amt": "0", "ovrs_tot_pfls": "0"}, "positions": []}, "US"
    )
    assert us["source"] == "broker" and us["currency"] == "USD" and us["positions"] == []
    # domestic: summary is a LIST
    kr = agent._broker_portfolio(
        {"summary": [{"dnca_tot_amt": "10000000", "tot_evlu_amt": "10000000",
                      "evlu_pfls_smtl_amt": "0", "scts_evlu_amt": "0"}], "positions": []},
        "KR",
    )
    assert kr["cash"] == 10000000 and kr["equity"] == 10000000


def test_us_mock_prices_have_cents(tmp_path):
    bars = KisClient(Settings(kis_env="mock", data_dir=tmp_path)).daily_prices("AAPL", "US", days=30)
    assert len(bars) == 30
    # at least some bars carry sub-dollar precision
    assert any(round(b.close) != b.close for b in bars)
