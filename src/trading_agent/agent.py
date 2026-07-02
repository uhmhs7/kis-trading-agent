from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from . import markets
from .config import Settings
from .indicators import atr, latest, percent_change, rsi, sma
from .kis_client import KisClient, PriceBar, Quote, bars_to_dicts, quote_to_dict
from .llm_agent import LLMAgent, LLMUnavailable
from .portfolio import Portfolio
from .risk import OrderDraft, RiskManager
from .storage import JsonStore

logger = logging.getLogger("trading_agent.agent")


SYMBOL_RE = re.compile(r"\b(?:Q?\d{6})\b")


class TradingAgent:
    def __init__(self, client: KisClient, risk: RiskManager, store: JsonStore, settings: Settings):
        self.client = client
        self.risk = risk
        self.store = store
        self.settings = settings
        self.portfolio = Portfolio(store, settings)
        self.llm = LLMAgent(self, settings)

    @staticmethod
    def _today() -> str:
        return datetime.now().date().isoformat()

    def llm_config(self) -> Dict[str, Any]:
        """Effective model + thinking (runtime UI override over env defaults)."""
        cfg = self.store.read_config()
        model = cfg.get("model") or self.settings.anthropic_model
        thinking = cfg.get("thinking")
        if thinking is None:
            thinking = self.settings.anthropic_thinking
        return {"model": model, "thinking": bool(thinking)}

    def auto_config(self) -> Dict[str, Any]:
        """Auto-trading toggles (runtime, store-backed)."""
        cfg = self.store.read_config()
        # LLM autopilot makes autonomous paid API calls, so it's effective only when
        # a dashboard token is configured (footgun guard: removing the token reverts
        # the scheduler to free rule-based mode).
        llm_mode = bool(cfg.get("auto_pilot_llm", False)) and bool(self.settings.dashboard_token)
        return {
            "auto_trade": bool(cfg.get("auto_trade", False)),
            "auto_pilot": bool(cfg.get("auto_pilot", False)),
            "auto_pilot_llm": llm_mode,
            "auto_pilot_interval": int(cfg.get("auto_pilot_interval", 300)),
        }

    def auto_execute(
        self, symbol: str, side: str, quantity: int, limit_price: float, market: Optional[str] = None
    ) -> Dict[str, Any]:
        """Preview + execute in one step (no human approval click).

        dry_run is decided by the server, never the caller: mock always simulates;
        paper places a real (fake-money) broker order; prod places a real-money
        order only when the live-order lock is open. All risk gates still apply.
        The confirm phrase is auto-supplied (there is no human in the loop).
        """
        symbol, market = markets.normalize_symbol(symbol, market)
        dry_run = self.settings.is_mock  # paper/prod attempt a real broker order
        preview = self.preview_order(symbol, side, quantity, limit_price, dry_run=dry_run, market=market)
        approval = preview["approval"]
        if approval["status"] != "pending":
            return {
                "status": approval["status"],
                "symbol": symbol,
                "blocks": approval["risk_check"]["blocks"],
            }
        result = self.execute_order(approval["id"], confirm_text=self.settings.live_confirm_text)
        return {
            "status": result["approval"]["status"],
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "dry_run": dry_run,
            "realized_pnl": result.get("realized_pnl"),
        }

    def _current_price(self, symbol: str, market: str = markets.KR) -> float:
        return self.client.quote(symbol, market).price

    def _account_context(self, draft: OrderDraft, quote: Quote) -> Dict[str, Any]:
        market = draft.market
        price = quote.price or draft.limit_price
        # Daily-loss limit tracks orders this app placed today (KIS balance has no
        # realized-P&L field), regardless of env.
        realized_today = self.portfolio.realized_today(self._today(), market)
        # Equity + held position value: REAL account in paper/prod, config base in mock.
        equity = self.settings.market_config(market).base_equity
        position_value = self.portfolio.position(market=market, symbol=draft.symbol)["qty"] * price
        if not self.settings.is_mock:
            try:
                broker = self._broker_portfolio(self.client.balance(market), market)
                if broker["equity"]:
                    equity = broker["equity"]
                held = next((p for p in broker["positions"] if p["symbol"] == draft.symbol), None)
                position_value = held["market_value"] if held else 0
            except Exception as exc:
                logger.warning("real-account context fetch failed, using fallback: %s", exc)
        return {"realized_today": realized_today, "equity": equity, "position_value": position_value}

    def analyze(self, symbol: str, market: Optional[str] = None, days: int = 130) -> Dict[str, Any]:
        symbol, market = markets.normalize_symbol(symbol, market)
        quote = self.client.quote(symbol, market)
        bars = self.client.daily_prices(symbol, market, days=days)
        if len(bars) < 20:
            raise ValueError("At least 20 daily bars are required for analysis.")
        metrics = self._metrics(bars, market)
        score, signals = self._score(quote, bars, metrics)
        action = self._action(score)
        risk_plan = self._risk_plan(quote, metrics, market)
        report = {
            "symbol": symbol,
            "name": quote.name,
            "market": market,
            "currency": quote.currency,
            "environment": self.settings.kis_env,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "quote": quote_to_dict(quote),
            "metrics": metrics,
            "score": score,
            "action": action,
            "signals": signals,
            "risk_plan": risk_plan,
            "recent_bars": bars_to_dicts(bars[-130:]),  # full window for the chart
            "disclaimer": "투자 판단 보조용 분석이며 수익을 보장하지 않습니다.",
        }
        self.store.append_log(
            "analysis", {"symbol": symbol, "market": market, "action": action["label"], "score": score}
        )
        return report

    def price_history(self, symbol: str, market: Optional[str] = None, days: int = 400) -> Dict[str, Any]:
        """Daily OHLCV bars for the chart. Longer windows are fetched on demand
        (the report's default analyze stays light). Recently-listed names simply
        return fewer bars — the client paginates until KIS/data runs out."""
        symbol, market = markets.normalize_symbol(symbol, market)
        days = max(20, min(int(days), 8000))
        bars = self.client.daily_prices(symbol, market, days=days)
        return {
            "symbol": symbol,
            "market": market,
            "currency": markets.currency_of(market),
            "bars": bars_to_dicts(bars),
            "count": len(bars),
        }

    def screen(self, symbols: Iterable[str], market: Optional[str] = None) -> Dict[str, Any]:
        results = []
        errors = []
        for symbol in symbols:
            try:
                results.append(self.analyze(symbol, market))
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
        ranked = sorted(results, key=lambda item: item["score"], reverse=True)
        return {"results": ranked, "errors": errors}

    def preview_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        limit_price: float,
        dry_run: bool = True,
        market: Optional[str] = None,
    ) -> Dict[str, Any]:
        symbol, market = markets.normalize_symbol(symbol, market)
        side = side.lower().strip()
        quote = self.client.quote(symbol, market)
        draft = OrderDraft(
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            limit_price=float(limit_price),
            dry_run=bool(dry_run),
            market=market,
        )
        check = self.risk.check_order(draft, quote=quote, account=self._account_context(draft, quote))
        approval = self.risk.build_approval(draft, check)
        self.store.save_approval(approval)
        self.store.append_log(
            "order_preview",
            {"approval_id": approval["id"], "symbol": symbol, "market": market, "allowed": check.allowed},
        )
        return {"approval": approval, "quote": quote_to_dict(quote)}

    def execute_order(self, approval_id: str, confirm_text: str = "") -> Dict[str, Any]:
        approval = self.store.get_approval(approval_id)
        if not approval:
            raise ValueError("승인 요청을 찾을 수 없습니다.")
        if approval["status"] != "pending":
            raise ValueError(f"이미 처리된 승인 요청입니다: {approval['status']}")
        draft = OrderDraft(**approval["draft"])
        # Only PROD moves real money → only prod real orders need the confirm phrase.
        needs_confirm = not draft.dry_run and self.settings.is_prod

        # Re-validate against the *current* market and settings — the approval was
        # built at preview time and its price/risk snapshot is stale by now.
        quote = self.client.quote(draft.symbol, draft.market)
        check = self.risk.check_order(draft, quote=quote, account=self._account_context(draft, quote))
        if not check.allowed:
            raise ValueError("리스크 체크를 통과하지 못한 주문입니다: " + "; ".join(check.blocks))

        if needs_confirm and confirm_text != self.settings.live_confirm_text:
            raise ValueError("실전 주문 확인 문구가 일치하지 않습니다.")

        # Atomically claim the approval so a concurrent double-submit can't place
        # the order twice.
        def _claim(current: Dict[str, Any]) -> Dict[str, Any]:
            if current["status"] != "pending":
                raise ValueError(f"이미 처리된 승인 요청입니다: {current['status']}")
            return {"status": "executing"}

        self.store.mutate_approval(approval_id, _claim)

        try:
            if draft.dry_run:
                response = {
                    "rt_cd": "0",
                    "msg1": "dry-run order recorded",
                    "output": {
                        "ODNO": f"DRY-{approval_id[:10].upper()}",
                        "PDNO": draft.symbol,
                        "ORD_QTY": str(draft.quantity),
                        "ORD_UNPR": str(draft.limit_price),
                    },
                }
            else:
                response = self.client.cash_order(
                    side=draft.side,
                    symbol=draft.symbol,
                    quantity=draft.quantity,
                    limit_price=draft.limit_price,
                    order_type=draft.order_type,
                    market=draft.market,
                )
        except Exception as exc:
            self.store.update_approval(
                approval_id,
                status="failed",
                error=str(exc),
                failed_at=datetime.now().isoformat(timespec="seconds"),
            )
            raise

        # Record the fill into the simulated portfolio: in mock everything is
        # simulated, so always record; in paper/prod only real (non-dry-run) fills.
        realized_pnl = None
        if self.settings.is_mock or not draft.dry_run:
            fill = self.portfolio.record_fill(
                draft.symbol, draft.side, draft.quantity, draft.limit_price, self._today(), draft.market
            )
            realized_pnl = fill["realized_pnl"]

        updated = self.store.update_approval(
            approval_id,
            status="executed",
            executed_at=datetime.now().isoformat(timespec="seconds"),
            risk_check=asdict(check),
            broker_response=response,
            realized_pnl=realized_pnl,
        )
        self.store.append_log(
            "order_execute",
            {
                "approval_id": approval_id,
                "symbol": draft.symbol,
                "side": draft.side,
                "quantity": draft.quantity,
                "dry_run": draft.dry_run,
                "realized_pnl": realized_pnl,
                "response": {"rt_cd": response.get("rt_cd"), "msg1": response.get("msg1")},
            },
        )
        return {"approval": updated, "broker_response": response, "realized_pnl": realized_pnl}

    def balance(self, market: Optional[str] = None) -> Dict[str, Any]:
        market = (market or markets.KR).upper()
        data = self.client.balance(market)
        if self.settings.is_mock:
            # Mock has no real broker — surface the simulated portfolio.
            portfolio = self.portfolio.snapshot(
                lambda sym: self._current_price(sym, market), self._today(), market
            )
            data["positions"] = portfolio["positions"]
        else:
            # paper/prod — surface the REAL KIS account balance/positions.
            portfolio = self._broker_portfolio(data, market)
        data["agent_portfolio"] = portfolio
        data["market"] = market
        data["currency"] = portfolio["currency"]
        self.store.append_log(
            "balance",
            {"environment": self.settings.kis_env, "market": market, "source": portfolio.get("source", "sim")},
        )
        return data

    def _broker_portfolio(self, data: Dict[str, Any], market: str) -> Dict[str, Any]:
        """Map a KIS inquire-balance response into the portfolio display shape.

        KIS returns the summary as a LIST for domestic but a DICT for overseas, so
        normalize both shapes.
        """
        currency = markets.currency_of(market)
        summary = data.get("summary") or {}
        if isinstance(summary, list):
            summary = summary[0] if summary else {}
        rows = data.get("positions") or []
        positions: List[Dict[str, Any]] = []
        if market == markets.US:
            for p in rows:
                qty = _num(p.get("ovrs_cblc_qty"))
                if qty <= 0:
                    continue
                positions.append({
                    "symbol": p.get("ovrs_pdno"), "name": p.get("ovrs_item_name"), "qty": qty,
                    "avg_cost": _num(p.get("pchs_avg_pric")), "price": _num(p.get("now_pric2")),
                    "market_value": _num(p.get("ovrs_stck_evlu_amt")),
                    "unrealized_pnl": _num(p.get("frcr_evlu_pfls_amt")),
                    "unrealized_pct": _num(p.get("evlu_pfls_rt")),
                })
            # The overseas balance API returns P&L only (no cash/total-asset field).
            market_value = sum(p["market_value"] for p in positions)
            cash = _num(summary.get("frcr_dncl_amt1"))  # 외화예수금 (often absent → 0)
            unrealized = _num(summary.get("tot_evlu_pfls_amt") or summary.get("ovrs_tot_pfls"))
            equity = cash + market_value
        else:
            for p in rows:
                qty = _num(p.get("hldg_qty"))
                if qty <= 0:
                    continue
                positions.append({
                    "symbol": p.get("pdno"), "name": p.get("prdt_name"), "qty": qty,
                    "avg_cost": _num(p.get("pchs_avg_pric")), "price": _num(p.get("prpr")),
                    "market_value": _num(p.get("evlu_amt")),
                    "unrealized_pnl": _num(p.get("evlu_pfls_amt")),
                    "unrealized_pct": _num(p.get("evlu_pfls_rt")),
                })
            cash = _num(summary.get("dnca_tot_amt"))
            equity = _num(summary.get("tot_evlu_amt"))
            market_value = _num(summary.get("scts_evlu_amt"))
            unrealized = _num(summary.get("evlu_pfls_smtl_amt"))
        positions.sort(key=lambda x: x["market_value"], reverse=True)
        return {
            "source": "broker",
            "currency": currency,
            "cash": cash,
            "market_value": market_value,
            "equity": equity,
            "unrealized_pnl": unrealized,
            "realized_pnl_today": None,  # not provided by the balance API
            "realized_pnl_total": None,
            "positions": positions,
        }

    def chat(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        text = message.strip()
        if not text:
            return {"kind": "empty", "message": "메시지를 입력해 주세요."}
        if self.llm.available:
            try:
                result = self.llm.chat(text, history=history)
                self.store.append_log(
                    "chat",
                    {"mode": "llm", "tool_calls": result.get("tool_calls", [])},
                )
                return result
            except LLMUnavailable:
                pass
            except Exception as exc:  # fall back to the deterministic router on any LLM error
                logger.warning("LLM chat failed, falling back to keyword router: %s", exc)
        result = self._keyword_chat(text)
        self.store.append_log("chat", {"mode": "keyword", "kind": result.get("kind")})
        return result

    # --- Session-backed chat (multi-turn persistence) ----------------------

    def _load_history(self, session_id: str) -> List[Dict[str, str]]:
        conv = self.store.get_conversation(session_id)
        if not conv:
            return []
        return [{"role": m["role"], "content": m["content"]} for m in conv.get("messages", [])]

    @staticmethod
    def _assistant_text(result: Dict[str, Any]) -> str:
        if result.get("message"):
            return result["message"]
        kind = result.get("kind")
        return {
            "balance": "잔고를 불러왔습니다.",
            "analysis": "분석을 완료했습니다.",
            "screen": "스캔을 완료했습니다.",
        }.get(kind, "요청을 처리했습니다.")

    @staticmethod
    def _keyword_artifacts(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        if result.get("data") is not None and result.get("kind"):
            return [{"type": result["kind"], "data": result["data"]}]
        return []

    def chat_session(self, message: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        text = (message or "").strip()
        session_id = session_id or uuid.uuid4().hex
        if not text:
            return {"kind": "empty", "message": "메시지를 입력해 주세요.", "session_id": session_id}
        history = self._load_history(session_id)
        self.store.append_message(session_id, "user", text)
        result = self.chat(text, history=history)
        self.store.append_message(session_id, "assistant", self._assistant_text(result))
        result["session_id"] = session_id
        return result

    def chat_stream_session(self, message: str, session_id: Optional[str] = None):
        """Generator yielding chat events; persists the turn and a session_id."""
        text = (message or "").strip()
        session_id = session_id or uuid.uuid4().hex
        if not text:
            yield {"type": "done", "session_id": session_id, "message": "메시지를 입력해 주세요.",
                   "artifacts": [], "tool_calls": []}
            return
        history = self._load_history(session_id)
        self.store.append_message(session_id, "user", text)

        final: Optional[Dict[str, Any]] = None
        if self.llm.available:
            try:
                for event in self.llm.chat_stream(text, history=history):
                    if event.get("type") == "done":
                        final = event
                    else:
                        yield event
            except Exception as exc:  # fall back to the keyword router on any LLM error
                logger.warning("LLM stream failed, falling back to keyword router: %s", exc)
                final = None
        if final is None:
            result = self._keyword_chat(text)
            final = {
                "type": "done",
                "message": self._assistant_text(result),
                "artifacts": self._keyword_artifacts(result),
                "tool_calls": [],
            }

        final["session_id"] = session_id
        self.store.append_message(session_id, "assistant", final.get("message", ""))
        self.store.append_log(
            "chat",
            {"mode": "llm-stream" if self.llm.available else "keyword", "tool_calls": final.get("tool_calls", [])},
        )
        yield final

    def _keyword_chat(self, text: str) -> Dict[str, Any]:
        if "잔고" in text:
            return {"kind": "balance", "data": self.balance()}
        symbols = SYMBOL_RE.findall(text.upper())
        if symbols:
            analysis = self.analyze(symbols[0])
            action = analysis["action"]["label"]
            price = analysis["quote"]["price"]
            return {
                "kind": "analysis",
                "message": f"{symbols[0]} 현재가 {price:,}원, 판단은 {action}입니다.",
                "data": analysis,
            }
        if "스캔" in text or "관심" in text:
            return {"kind": "screen", "data": self.screen(self.settings.default_watchlist)}
        return {
            "kind": "fallback",
            "message": "종목코드 6자리, 잔고, 관심종목 스캔 중 하나로 요청해 주세요. "
            "(자연어 대화를 쓰려면 ANTHROPIC_API_KEY를 설정하세요.)",
        }

    def _metrics(self, bars: List[PriceBar], market: str = markets.KR) -> Dict[str, Any]:
        pdig = 2 if market == markets.US else 0  # price precision: cents for US
        closes = [bar.close for bar in bars]
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        volumes = [bar.volume for bar in bars]
        n = len(closes)
        sma5 = sma(closes, 5)
        sma20 = sma(closes, 20)
        sma60 = sma(closes, 60)
        rsi14 = rsi(closes, 14)
        atr14 = atr(highs, lows, closes, 14)
        # Baselines that exclude the current bar so today isn't compared to itself.
        avg_volume20 = (sum(volumes[-21:-1]) / 20) if n >= 21 else (sum(volumes[-20:]) / 20)
        return_base = closes[-21] if n >= 21 else closes[-20]
        high20 = max(highs[-20:])  # inclusive, for display
        low20 = min(lows[-20:])
        prior_high20 = max(highs[-21:-1]) if n >= 21 else None  # excludes today, for breakout
        return {
            "close": closes[-1],
            "sma5": _round(latest(sma5), pdig),
            "sma20": _round(latest(sma20), pdig),
            "sma60": _round(latest(sma60), pdig),
            "rsi14": _round(latest(rsi14), 2),
            "atr14": _round(latest(atr14), pdig),
            "volume_ratio20": _round(volumes[-1] / avg_volume20, 2) if avg_volume20 else 0,
            "return20d_pct": _round(percent_change(return_base, closes[-1]), 2),
            "high20": _round(high20, pdig),
            "low20": _round(low20, pdig),
            "prior_high20": _round(prior_high20, pdig) if prior_high20 is not None else None,
            "previous_sma5": _round(sma5[-2], pdig) if len(sma5) > 1 else None,
            "previous_sma20": _round(sma20[-2], pdig) if len(sma20) > 1 else None,
        }

    def _score(self, quote: Quote, bars: List[PriceBar], metrics: Dict[str, Any]) -> tuple[int, List[Dict[str, Any]]]:
        score = 0
        signals: List[Dict[str, Any]] = []

        def add(name: str, impact: int, detail: str) -> None:
            nonlocal score
            score += impact
            signals.append({"name": name, "impact": impact, "detail": detail})

        price = quote.price or bars[-1].close
        sma5_value = metrics.get("sma5")
        sma20_value = metrics.get("sma20")
        sma60_value = metrics.get("sma60")
        rsi14_value = metrics.get("rsi14")

        if sma20_value and sma60_value and price > sma20_value > sma60_value:
            add("정배열 추세", 2, "현재가가 20일선과 60일선 위에 있습니다.")
        elif sma20_value and price < sma20_value:
            add("단기 추세 이탈", -1, "현재가가 20일선 아래에 있습니다.")

        if (
            sma5_value
            and sma20_value
            and metrics.get("previous_sma5")
            and metrics.get("previous_sma20")
            and sma5_value > sma20_value
            and metrics["previous_sma5"] <= metrics["previous_sma20"]
        ):
            add("골든크로스", 2, "5일선이 20일선을 상향 돌파했습니다.")

        if rsi14_value is not None:
            if 45 <= rsi14_value <= 65:
                add("RSI 중립", 1, "과열 없이 추세 확인 구간입니다.")
            elif rsi14_value > 75:
                add("RSI 과열", -1, "단기 과열 가능성이 있습니다.")
            elif rsi14_value < 35:
                add("RSI 침체", 1, "반등 후보이나 추세 확인이 필요합니다.")

        if metrics.get("volume_ratio20", 0) >= 1.5:
            add("거래량 증가", 1, "20일 평균 대비 거래량이 늘었습니다.")
        prior_high20 = metrics.get("prior_high20")
        if prior_high20 is not None and price > prior_high20:
            add("20일 고가 돌파", 2, "직전 20거래일 고가를 상향 돌파했습니다.")
        if metrics.get("return20d_pct", 0) < -8:
            add("20일 약세", -1, "최근 20거래일 수익률이 부진합니다.")

        return score, signals

    def _action(self, score: int) -> Dict[str, str]:
        if score >= 4:
            return {"label": "BUY_CANDIDATE", "tone": "positive", "text": "매수 후보"}
        if score >= 1:
            return {"label": "WATCH", "tone": "neutral", "text": "관망"}
        return {"label": "AVOID_OR_WAIT", "tone": "caution", "text": "대기"}

    def _risk_plan(self, quote: Quote, metrics: Dict[str, Any], market: str = markets.KR) -> Dict[str, Any]:
        mc = self.settings.market_config(market)
        pdig = 2 if market == markets.US else 0
        floor = 0.01 if market == markets.US else 1
        entry = float(quote.price or metrics["close"])
        atr14 = metrics.get("atr14") or max(floor, entry * 0.025)
        stop = max(floor, min(entry * 0.97, entry - atr14 * 1.2))
        risk_per_share = max(floor, entry - stop)
        target = entry + risk_per_share * 2
        # Honest 0 when a single share already exceeds the per-order cap, so the plan
        # never advertises a quantity the risk gate would block.
        max_qty = int(mc.max_order // entry) if entry > 0 else 0
        # Risk-based size: keep loss-at-stop within the daily loss limit, then cap by
        # the per-order limit. This is the conservative default the ticket prefills.
        loss_limit = mc.daily_loss_limit
        if max_qty <= 0:
            suggested_qty = 0
        else:
            loss_based = int(loss_limit // risk_per_share) if (loss_limit > 0 and risk_per_share > 0) else max_qty
            suggested_qty = max(1, min(max_qty, loss_based))
        return {
            "entry_reference": _round(entry, pdig),
            "stop_loss": _round(stop, pdig),
            "take_profit": _round(target, pdig),
            "risk_per_share": _round(risk_per_share, pdig),
            "suggested_quantity": suggested_qty,
            "max_quantity_by_order_limit": max_qty,
            "affordable_within_order_limit": max_qty > 0,
            "currency": mc.currency,
            "daily_loss_limit": loss_limit,
            "max_order": mc.max_order,
        }


def _round(value: Any, digits: int = 0) -> Any:
    if value is None:
        return None
    rounded = round(float(value), digits)
    return int(rounded) if digits == 0 else rounded


def _num(value: Any, default: float = 0.0) -> float:
    """Parse a KIS numeric string (e.g. '10000000', '326,000.00') to a number."""
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return default

