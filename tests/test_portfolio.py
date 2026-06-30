import pytest

from trading_agent.agent import TradingAgent
from trading_agent.config import Settings
from trading_agent.kis_client import KisClient
from trading_agent.portfolio import Portfolio
from trading_agent.risk import RiskManager
from trading_agent.storage import JsonStore


def make_agent(tmp_path, **overrides):
    settings = Settings(kis_env="mock", data_dir=tmp_path, **overrides)
    return TradingAgent(KisClient(settings), RiskManager(settings), JsonStore(tmp_path), settings)


def _buy(agent, symbol, qty, price):
    preview = agent.preview_order(symbol, "buy", qty, price, dry_run=True)
    return agent.execute_order(preview["approval"]["id"])


def _sell(agent, symbol, qty, price):
    preview = agent.preview_order(symbol, "sell", qty, price, dry_run=True)
    return agent.execute_order(preview["approval"]["id"])


# --- Portfolio unit ---------------------------------------------------------


def test_average_cost_and_realized_pnl(tmp_path):
    settings = Settings(kis_env="mock", data_dir=tmp_path, base_equity_krw=1_000_000)
    pf = Portfolio(JsonStore(tmp_path), settings)
    pf.record_fill("005930", "buy", 10, 1000, "2026-06-28")
    pf.record_fill("005930", "buy", 10, 2000, "2026-06-28")
    pos = pf.position("005930")
    assert pos["qty"] == 20
    assert pos["avg_cost"] == 1500

    result = pf.record_fill("005930", "sell", 10, 2500, "2026-06-28")
    assert result["realized_pnl"] == (2500 - 1500) * 10
    assert pf.realized_today("2026-06-28") == 10_000
    assert pf.realized_today("2026-06-29") == 0
    assert pf.position("005930")["qty"] == 10


# --- Agent integration ------------------------------------------------------


def test_buy_then_sell_tracks_position_and_pnl(tmp_path):
    agent = make_agent(tmp_path, max_order_krw=10_000_000, max_position_pct=1.0, base_equity_krw=10_000_000)
    _buy(agent, "005930", 10, 50_000)
    snapshot = agent.balance()["agent_portfolio"]
    assert any(p["symbol"] == "005930" and p["qty"] == 10 for p in snapshot["positions"])

    result = _sell(agent, "005930", 10, 40_000)
    assert result["realized_pnl"] == -100_000
    snapshot = agent.balance()["agent_portfolio"]
    assert snapshot["realized_pnl_today"] == -100_000
    assert snapshot["positions"] == []


def test_daily_loss_limit_blocks_new_orders(tmp_path):
    agent = make_agent(
        tmp_path,
        max_order_krw=10_000_000,
        max_position_pct=1.0,
        daily_loss_limit_krw=10_000,
        base_equity_krw=10_000_000,
    )
    _buy(agent, "005930", 10, 50_000)
    _sell(agent, "005930", 10, 40_000)  # realize -100,000, well past the 10,000 limit
    blocked = agent.preview_order("000660", "buy", 1, 1_000, dry_run=True)
    assert blocked["approval"]["status"] == "blocked"
    assert any("실현 손실" in b for b in blocked["approval"]["risk_check"]["blocks"])


def test_position_weight_limit_blocks_oversized_buy(tmp_path):
    agent = make_agent(tmp_path, max_order_krw=10_000_000, max_position_pct=0.20, base_equity_krw=1_000_000)
    # 10 * 50,000 = 500,000 = 50% of equity, over the 20% cap.
    preview = agent.preview_order("005930", "buy", 10, 50_000, dry_run=True)
    assert preview["approval"]["status"] == "blocked"
    assert any("비중" in b for b in preview["approval"]["risk_check"]["blocks"])


def test_suggested_quantity_respects_order_limit(tmp_path):
    agent = make_agent(tmp_path, max_order_krw=100_000, daily_loss_limit_krw=10_000)
    plan = agent.analyze("005930")["risk_plan"]
    assert plan["suggested_quantity"] >= 1
    assert plan["suggested_quantity"] <= plan["max_quantity_by_order_limit"]


def test_balance_exposes_agent_portfolio(tmp_path):
    agent = make_agent(tmp_path)
    portfolio = agent.balance()["agent_portfolio"]
    for key in ("equity", "cash", "realized_pnl_today", "realized_pnl_total", "positions"):
        assert key in portfolio
