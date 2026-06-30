"""Background auto-trading scheduler.

Periodically scans the watchlist and trades autonomously via agent.auto_execute
(which decides dry_run by env: mock simulates, paper = fake money, prod = real and
lock-gated). Two modes: deterministic rules (default, no LLM cost) or LLM-driven
(auto_pilot_llm). All orders pass the normal risk gates.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List

logger = logging.getLogger("trading_agent.autopilot")

# Rule-mode exit thresholds (% from average cost).
STOP_PCT = -5.0
TARGET_PCT = 10.0


class AutoPilot:
    def __init__(self, get_agent: Callable[[], Any], get_settings: Callable[[], Any], store: Any):
        self._get_agent = get_agent
        self._get_settings = get_settings
        self._store = store
        self._thread = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autopilot", daemon=True)
        self._thread.start()
        logger.info("autopilot started")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5)  # wait for the in-flight cycle to bail out
        logger.info("autopilot stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("autopilot cycle error: %s", exc)
            interval = max(15, int(self._get_agent().auto_config().get("auto_pilot_interval", 300)))
            for _ in range(interval):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def cycle(self) -> List[Dict[str, Any]]:
        agent = self._get_agent()
        settings = self._get_settings()
        cfg = agent.auto_config()
        symbols = list(settings.default_watchlist)

        candidates: List[Dict[str, Any]] = []
        for symbol in symbols:
            if self._stop.is_set():  # responsive stop mid-cycle (no stray real-account calls)
                return []
            try:
                report = agent.analyze(symbol)
            except Exception as exc:
                logger.warning("autopilot analyze %s: %s", symbol, exc)
                continue
            market = report.get("market", "KR")
            price = report["quote"]["price"]
            pos = agent.portfolio.position(symbol, market)
            held = pos["qty"] > 0
            avg = pos.get("avg_cost", 0)
            unrealized_pct = round((price - avg) / avg * 100, 2) if (held and avg) else 0.0
            candidates.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "price": price,
                    "action": report["action"]["label"],
                    "score": report["score"],
                    "held": held,
                    "qty": pos["qty"],
                    "suggested_quantity": report["risk_plan"]["suggested_quantity"],
                    "unrealized_pct": unrealized_pct,
                }
            )

        decisions = self._llm_decisions(agent, candidates) if cfg.get("auto_pilot_llm") else self._rule_decisions(candidates)

        executed = []
        for d in decisions:
            if self._stop.is_set():
                break
            try:
                result = agent.auto_execute(d["symbol"], d["side"], d["quantity"], d["price"], d["market"])
                executed.append({"side": d["side"], "symbol": d["symbol"], "status": result.get("status")})
            except Exception as exc:
                logger.warning("autopilot execute %s: %s", d["symbol"], exc)
        self._store.append_log(
            "autopilot",
            {"mode": "llm" if cfg.get("auto_pilot_llm") else "rule", "executed": executed},
        )
        return executed

    @staticmethod
    def _rule_decisions(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        decisions = []
        for c in candidates:
            if not c["held"] and c["action"] == "BUY_CANDIDATE" and c["suggested_quantity"] > 0:
                decisions.append({**c, "side": "buy", "quantity": c["suggested_quantity"]})
            elif c["held"] and (c["unrealized_pct"] <= STOP_PCT or c["unrealized_pct"] >= TARGET_PCT):
                decisions.append({**c, "side": "sell", "quantity": c["qty"]})
        return decisions

    def _llm_decisions(self, agent: Any, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compact = [
            {k: c[k] for k in ("symbol", "market", "action", "score", "price", "held", "unrealized_pct")}
            for c in candidates
        ]
        picks = agent.llm.decide_trades(compact)
        by_symbol = {c["symbol"]: c for c in candidates}
        decisions = []
        for pick in picks:
            c = by_symbol.get(pick.get("symbol"))
            if not c:
                continue
            side = pick["side"]
            qty = c["qty"] if side == "sell" else c["suggested_quantity"]
            if qty and qty > 0:
                decisions.append({**c, "side": side, "quantity": qty})
        return decisions
