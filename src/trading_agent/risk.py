from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import markets
from .config import Settings
from .kis_client import Quote


@dataclass
class OrderDraft:
    symbol: str
    side: str
    quantity: int
    limit_price: float
    order_type: str = "00"
    dry_run: bool = True
    market: str = "KR"

    @property
    def notional(self) -> float:
        return self.quantity * self.limit_price


@dataclass
class RiskCheck:
    allowed: bool
    blocks: List[str]
    warnings: List[str]
    constraints: Dict[str, Any]


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check_order(
        self,
        draft: OrderDraft,
        quote: Optional[Quote] = None,
        account: Optional[Dict[str, Any]] = None,
    ) -> RiskCheck:
        blocks: List[str] = []
        warnings: List[str] = []
        mc = self.settings.market_config(draft.market)
        currency = mc.currency

        def money(amount: float) -> str:
            return markets.format_money(amount, currency)

        if draft.side not in {"buy", "sell"}:
            blocks.append("매수/매도 구분이 올바르지 않습니다.")
        if draft.quantity <= 0:
            blocks.append("주문 수량은 1주 이상이어야 합니다.")
        if draft.limit_price <= 0:
            blocks.append("시장가 주문은 막아두었습니다. 지정가를 입력하세요.")
        if draft.order_type != "00":
            blocks.append("초기 버전은 지정가 주문(ORD_DVSN=00)만 허용합니다.")
        if draft.notional > mc.max_order:
            blocks.append(
                f"주문 금액 {money(draft.notional)}이 1회 한도 {money(mc.max_order)}을 초과합니다."
            )
        if self.settings.allowed_symbols and draft.symbol not in self.settings.allowed_symbols:
            blocks.append("허용 종목 목록에 없는 종목입니다.")
        # Only PROD moves real money, so only prod real orders need the lock open.
        # mock = simulation, paper = broker call with fake money (no lock needed).
        if self.settings.is_prod and not draft.dry_run and not self.settings.allow_live_orders:
            blocks.append("실주문 잠금이 켜져 있습니다. KIS_ALLOW_LIVE_ORDERS=true가 필요합니다.")

        # Realized daily-loss limit and single-name position-weight limit.
        realized_today = None
        if account is not None:
            realized_today = account.get("realized_today", 0)
            limit = mc.daily_loss_limit
            if limit > 0 and realized_today <= -limit:
                blocks.append(
                    f"오늘 실현 손실 {money(abs(realized_today))}이 일일 한도 {money(limit)}에 도달했습니다."
                )
            equity = account.get("equity", 0)
            pct = mc.max_position_pct
            if draft.side == "buy" and pct > 0 and equity > 0:
                exposure = account.get("position_value", 0) + draft.notional
                if exposure > equity * pct:
                    blocks.append(
                        f"단일 종목 비중 {exposure / equity * 100:.1f}%가 한도 {pct * 100:.0f}%를 초과합니다."
                    )

        if quote and quote.price > 0 and draft.limit_price > 0:
            diff_pct = abs(draft.limit_price - quote.price) / quote.price
            if diff_pct > 0.05:
                warnings.append("지정가가 현재가와 5% 이상 차이납니다.")
        if draft.notional > mc.max_order * 0.8:
            warnings.append("1회 주문 한도의 80% 이상을 사용합니다.")
        if draft.dry_run:
            warnings.append("드라이런 주문입니다. 증권사 주문 API를 호출하지 않습니다.")

        constraints = {
            "environment": self.settings.kis_env,
            "market": draft.market,
            "currency": currency,
            "max_order": mc.max_order,
            "daily_loss_limit": mc.daily_loss_limit,
            "max_position_pct": mc.max_position_pct,
            "live_orders_enabled": self.settings.allow_live_orders,
            "allowed_symbols": list(self.settings.allowed_symbols),
            "realized_pnl_today": realized_today,
        }
        return RiskCheck(allowed=not blocks, blocks=blocks, warnings=warnings, constraints=constraints)

    def build_approval(self, draft: OrderDraft, check: RiskCheck) -> Dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "status": "pending" if check.allowed else "blocked",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "draft": asdict(draft),
            "risk_check": asdict(check),
        }
