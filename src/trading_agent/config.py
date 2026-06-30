from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _csv(name: str, default: str = "") -> Tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip().upper() for item in raw.split(",") if item.strip())


_DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_DEFAULT_WATCHLIST = ("005930", "000660", "035420", "051910", "005380")


@dataclass(frozen=True)
class MarketLimits:
    currency: str
    max_order: float
    daily_loss_limit: float
    base_equity: float
    max_position_pct: float


@dataclass(frozen=True)
class Settings:
    # Static defaults — do NOT read env here. Use Settings.from_env() so values are
    # read at instantiation time, not once at module import (keeps tests and runtime
    # config deterministic).
    kis_env: str = "mock"
    app_key: str = ""
    app_secret: str = ""
    paper_app_key: str = ""
    paper_app_secret: str = ""
    account_no: str = ""
    paper_account_no: str = ""
    account_product_code: str = "01"
    hts_id: str = ""
    user_agent: str = _DEFAULT_USER_AGENT
    allow_live_orders: bool = False
    live_confirm_text: str = "EXECUTE_LIVE_ORDER"
    max_order_krw: int = 50_000
    daily_loss_limit_krw: int = 10_000
    max_position_pct: float = 0.20
    # Reference account equity used for the single-name position-weight check and
    # the mock portfolio's starting cash.
    base_equity_krw: int = 1_000_000
    # US (USD) account limits — the overseas counterpart of the KRW limits above.
    max_order_usd: float = 50.0
    daily_loss_limit_usd: float = 10.0
    base_equity_usd: float = 1_000.0
    allowed_symbols: Tuple[str, ...] = ()
    # Optional shared secret. When set, /api/orders/* and /api/balance require the
    # matching X-Dashboard-Token header. Leave empty for the open mock demo.
    dashboard_token: str = ""
    # LLM agent settings. Without an API key the chat endpoint falls back to the
    # deterministic keyword router so the mock demo still works offline.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    anthropic_thinking: bool = False  # adaptive thinking default (UI can override)
    llm_max_steps: int = 6
    default_watchlist: Tuple[str, ...] = _DEFAULT_WATCHLIST
    data_dir: Path = Path("data")
    token_cache_dir: Path = Path.home() / ".sk_aiagent"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            kis_env=os.getenv("KIS_ENV", "mock").strip().lower(),
            app_key=os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APP_SECRET", ""),
            paper_app_key=os.getenv("KIS_PAPER_APP_KEY", ""),
            paper_app_secret=os.getenv("KIS_PAPER_APP_SECRET", ""),
            account_no=os.getenv("KIS_ACCOUNT_NO", ""),
            paper_account_no=os.getenv("KIS_PAPER_ACCOUNT_NO", ""),
            account_product_code=os.getenv("KIS_ACCOUNT_PRODUCT_CODE", "01"),
            hts_id=os.getenv("KIS_HTS_ID", ""),
            user_agent=os.getenv("KIS_USER_AGENT", _DEFAULT_USER_AGENT),
            allow_live_orders=_bool("KIS_ALLOW_LIVE_ORDERS", False),
            live_confirm_text=os.getenv("KIS_LIVE_CONFIRM_TEXT", "EXECUTE_LIVE_ORDER"),
            max_order_krw=_int("KIS_MAX_ORDER_KRW", 50_000),
            daily_loss_limit_krw=_int("KIS_DAILY_LOSS_LIMIT_KRW", 10_000),
            max_position_pct=_float("KIS_MAX_POSITION_PCT", 0.20),
            base_equity_krw=_int("KIS_BASE_EQUITY_KRW", 1_000_000),
            max_order_usd=_float("KIS_MAX_ORDER_USD", 50.0),
            daily_loss_limit_usd=_float("KIS_DAILY_LOSS_LIMIT_USD", 10.0),
            base_equity_usd=_float("KIS_BASE_EQUITY_USD", 1_000.0),
            allowed_symbols=_csv("KIS_ALLOWED_SYMBOLS"),
            dashboard_token=os.getenv("DASHBOARD_TOKEN", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8"),
            anthropic_thinking=_bool("ANTHROPIC_THINKING", False),
            llm_max_steps=_int("LLM_MAX_STEPS", 6),
            default_watchlist=_csv("DEFAULT_WATCHLIST", "005930,000660,035420,051910,005380"),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            token_cache_dir=Path(
                os.getenv("KIS_TOKEN_CACHE_DIR", str(Path.home() / ".sk_aiagent"))
            ),
        )

    @property
    def is_mock(self) -> bool:
        return self.kis_env == "mock"

    @property
    def is_paper(self) -> bool:
        return self.kis_env == "paper"

    @property
    def is_prod(self) -> bool:
        return self.kis_env == "prod"

    @property
    def base_url(self) -> str:
        if self.is_paper:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def env_label(self) -> str:
        if self.is_prod:
            return "real"
        if self.is_paper:
            return "demo"
        return "mock"

    @property
    def active_app_key(self) -> str:
        return self.paper_app_key if self.is_paper else self.app_key

    @property
    def active_app_secret(self) -> str:
        return self.paper_app_secret if self.is_paper else self.app_secret

    @property
    def active_account_no(self) -> str:
        return self.paper_account_no if self.is_paper else self.account_no

    @property
    def has_api_credentials(self) -> bool:
        if self.is_mock:
            return True
        return bool(self.active_app_key and self.active_app_secret)

    @property
    def has_account(self) -> bool:
        if self.is_mock:
            return True
        return bool(self.active_account_no and self.account_product_code)

    def market_config(self, market: str) -> "MarketLimits":
        """Per-market currency and risk limits (KR=KRW, US=USD)."""
        if (market or "KR").upper() == "US":
            return MarketLimits(
                currency="USD",
                max_order=self.max_order_usd,
                daily_loss_limit=self.daily_loss_limit_usd,
                base_equity=self.base_equity_usd,
                max_position_pct=self.max_position_pct,
            )
        return MarketLimits(
            currency="KRW",
            max_order=self.max_order_krw,
            daily_loss_limit=self.daily_loss_limit_krw,
            base_equity=self.base_equity_krw,
            max_position_pct=self.max_position_pct,
        )

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def requires_dashboard_token(self) -> bool:
        return bool(self.dashboard_token)

    def validate_runtime(self) -> None:
        if self.kis_env not in {"mock", "paper", "prod"}:
            raise ValueError("KIS_ENV must be one of mock, paper, or prod.")
        if not self.has_api_credentials:
            raise ValueError("KIS API credentials are missing for the selected environment.")


def get_settings() -> Settings:
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.token_cache_dir.mkdir(parents=True, exist_ok=True)
    try:  # the token cache holds bearer tokens — keep the dir private
        os.chmod(settings.token_cache_dir, 0o700)
    except OSError:
        pass
    return settings

