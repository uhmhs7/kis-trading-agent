from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


Market = Literal["KR", "US"]


class AnalyzeRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=12, examples=["005930", "AAPL"])
    market: Optional[Market] = None  # auto-detected from the symbol when omitted


class ScreenRequest(BaseModel):
    symbols: List[str] = Field(..., max_length=30)
    market: Optional[Market] = None


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=2000)
    session_id: Optional[str] = Field(default=None, max_length=64)
    # Retained for backward compatibility; server now loads history from the
    # persisted conversation keyed by session_id.
    history: List[ChatTurn] = Field(default_factory=list, max_length=16)


class OrderPreviewRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=12)
    side: str = Field(..., max_length=8, examples=["buy"])
    quantity: int = Field(..., gt=0, le=1_000_000)
    limit_price: float = Field(..., gt=0, le=1_000_000_000)  # decimals allowed for USD
    dry_run: bool = True
    market: Optional[Market] = None


class OrderExecuteRequest(BaseModel):
    approval_id: str = Field(..., max_length=64)
    confirm_text: str = Field(default="", max_length=64)


class ConfigRequest(BaseModel):
    model: Optional[str] = Field(default=None, max_length=64)
    thinking: Optional[bool] = None
    environment: Optional[Literal["mock", "paper", "prod"]] = None
    dry_run_default: Optional[bool] = None
    max_order_krw: Optional[int] = Field(default=None, gt=0, le=1_000_000_000)
    max_order_usd: Optional[float] = Field(default=None, gt=0, le=10_000_000)
    allow_live_orders: Optional[bool] = None
    auto_trade: Optional[bool] = None  # conversational auto-execute
    auto_pilot: Optional[bool] = None  # background scheduler
    auto_pilot_llm: Optional[bool] = None  # scheduler uses the LLM vs deterministic rules
    auto_pilot_interval: Optional[int] = Field(default=None, ge=15, le=3600)

