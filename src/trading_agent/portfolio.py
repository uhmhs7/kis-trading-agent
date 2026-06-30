"""Simulated portfolio with per-market (KR/US) sub-ledgers.

Backed by the JsonStore positions doc, namespaced by market so KRW and USD
accounts track average cost, realized and unrealized P&L independently. Used to
enforce the daily-loss and single-name-weight limits and to show P&L.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from . import markets
from .config import Settings
from .storage import JsonStore


def _round_price(value: float, market: str) -> float:
    return round(value, 2) if market == markets.US else int(round(value))


class Portfolio:
    def __init__(self, store: JsonStore, settings: Settings):
        self.store = store
        self.settings = settings

    def _ensure(self, doc: Dict[str, Any], market: str) -> Dict[str, Any]:
        sub = doc.get(market)
        if sub is None:
            sub = {"cash": self.settings.market_config(market).base_equity, "positions": {}, "realized": []}
            doc[market] = sub
        sub.setdefault("positions", {})
        sub.setdefault("realized", [])
        if "cash" not in sub:
            sub["cash"] = self.settings.market_config(market).base_equity
        return sub

    def record_fill(
        self, symbol: str, side: str, qty: int, price: float, date: str, market: str = markets.KR
    ) -> Dict[str, Any]:
        """Apply a fill. Returns {'realized_pnl': number} (non-zero only for sells)."""
        result = {"realized_pnl": 0}

        def mutate(doc: Dict[str, Any]) -> None:
            sub = self._ensure(doc, market)
            positions = sub["positions"]
            pos = positions.get(symbol, {"qty": 0, "avg_cost": 0})
            if side == "buy":
                new_qty = pos["qty"] + qty
                if new_qty > 0:
                    pos["avg_cost"] = _round_price(
                        (pos["avg_cost"] * pos["qty"] + price * qty) / new_qty, market
                    )
                pos["qty"] = new_qty
                sub["cash"] = _round_price(sub["cash"] - qty * price, market)
            else:  # sell
                close_qty = min(qty, pos["qty"]) if pos["qty"] > 0 else 0
                pnl = _round_price((price - pos["avg_cost"]) * close_qty, market)
                result["realized_pnl"] = pnl
                pos["qty"] = max(0, pos["qty"] - qty)
                sub["cash"] = _round_price(sub["cash"] + qty * price, market)
                sub["realized"].append(
                    {"date": date, "symbol": symbol, "side": side, "qty": qty, "price": price, "pnl": pnl}
                )
            if pos["qty"] <= 0:
                positions.pop(symbol, None)
            else:
                positions[symbol] = pos

        self.store.mutate_positions(mutate)
        return result

    def position(self, symbol: str, market: str = markets.KR) -> Dict[str, Any]:
        sub = self.store.read_positions().get(market, {})
        return sub.get("positions", {}).get(symbol, {"qty": 0, "avg_cost": 0})

    def realized_today(self, today: str, market: str = markets.KR) -> float:
        sub = self.store.read_positions().get(market, {})
        return sum(r["pnl"] for r in sub.get("realized", []) if r.get("date") == today)

    def realized_total(self, market: str = markets.KR) -> float:
        sub = self.store.read_positions().get(market, {})
        return sum(r["pnl"] for r in sub.get("realized", []))

    def snapshot(self, price_fn: Callable[[str], float], today: str, market: str = markets.KR) -> Dict[str, Any]:
        sub = self.store.read_positions().get(market, {})
        cash = sub.get("cash", self.settings.market_config(market).base_equity)
        positions: List[Dict[str, Any]] = []
        market_total = 0.0
        unrealized_total = 0.0
        for sym, pos in sub.get("positions", {}).items():
            qty = pos["qty"]
            avg = pos["avg_cost"]
            price = price_fn(sym)
            market_value = qty * price
            unrealized = (price - avg) * qty
            market_total += market_value
            unrealized_total += unrealized
            positions.append(
                {
                    "symbol": sym,
                    "qty": qty,
                    "avg_cost": avg,
                    "price": price,
                    "market_value": _round_price(market_value, market),
                    "unrealized_pnl": _round_price(unrealized, market),
                    "unrealized_pct": round((price - avg) / avg * 100, 2) if avg else 0.0,
                }
            )
        positions.sort(key=lambda p: p["market_value"], reverse=True)
        realized_today = sum(r["pnl"] for r in sub.get("realized", []) if r.get("date") == today)
        return {
            "market": market,
            "currency": markets.currency_of(market),
            "cash": _round_price(cash, market),
            "market_value": _round_price(market_total, market),
            "equity": _round_price(cash + market_total, market),
            "unrealized_pnl": _round_price(unrealized_total, market),
            "realized_pnl_today": realized_today,
            "realized_pnl_total": self.realized_total(market),
            "positions": positions,
        }
