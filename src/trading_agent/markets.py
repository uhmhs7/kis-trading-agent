"""Market abstraction: KRX (KR) domestic and US overseas equities.

Holds symbol validation/detection, currency, KIS exchange codes, and a small
name<->ticker table per market so the agent can serve both 국내주식 and 미국주식.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

KR = "KR"
US = "US"
MARKETS = (KR, US)

CURRENCY = {KR: "KRW", US: "USD"}
CURRENCY_SYMBOL = {"KRW": "₩", "USD": "$"}

_KR_RE = re.compile(r"Q?\d{6}")
_US_RE = re.compile(r"[A-Z]{1,5}(\.[A-Z])?")

# US exchange codes differ between the price API (EXCD: NAS/NYS/AMS) and the
# trading API (OVRS_EXCG_CD: NASD/NYSE/AMEX). Map common tickers; default NASDAQ.
_US_EXCHANGE: Dict[str, str] = {}

_NASDAQ = (
    "AAPL MSFT NVDA AMZN GOOGL GOOG META TSLA AVGO NFLX AMD INTC ADBE COST PEP "
    "CSCO QCOM TXN AMAT MU PLTR SBUX ABNB PYPL MRVL"
).split()
_NYSE = (
    "JPM V MA UNH XOM JNJ WMT PG HD KO BAC DIS CVX ORCL CRM MCD NKE PFE BA GS "
    "BRK.B LLY ABBV"
).split()

for _t in _NASDAQ:
    _US_EXCHANGE[_t] = "NASD"
for _t in _NYSE:
    _US_EXCHANGE[_t] = "NYSE"

_TRADE_TO_PRICE_EXCD = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}

US_NAMES: Dict[str, str] = {
    "AAPL": "애플",
    "MSFT": "마이크로소프트",
    "NVDA": "엔비디아",
    "AMZN": "아마존",
    "GOOGL": "알파벳",
    "META": "메타",
    "TSLA": "테슬라",
    "NFLX": "넷플릭스",
    "AMD": "AMD",
    "AVGO": "브로드컴",
    "INTC": "인텔",
    "JPM": "JP모건",
    "V": "비자",
    "KO": "코카콜라",
    "DIS": "디즈니",
    "WMT": "월마트",
    "COST": "코스트코",
    "PLTR": "팔란티어",
}
_US_NAME_TO_TICKER = {name: ticker for ticker, name in US_NAMES.items()}


def detect_market(symbol: str) -> str:
    s = symbol.strip().upper()
    if _KR_RE.fullmatch(s):
        return KR
    if _US_RE.fullmatch(s):
        return US
    return KR


def normalize_symbol(symbol: str, market: Optional[str] = None) -> Tuple[str, str]:
    """Return (normalized_symbol, market). Raises ValueError on bad input."""
    s = symbol.strip().upper().replace(" ", "")
    market = (market or detect_market(s)).upper()
    if market == US:
        if not _US_RE.fullmatch(s):
            raise ValueError("미국 종목 티커는 1~5자리 알파벳입니다 (예: AAPL).")
        return s, US
    if not _KR_RE.fullmatch(s):
        raise ValueError("종목코드는 6자리 숫자 또는 Q+6자리 형식이어야 합니다.")
    return s, KR


def currency_of(market: str) -> str:
    return CURRENCY.get(market, "KRW")


def format_money(amount: float, currency: str) -> str:
    if currency == "USD":
        return f"${amount:,.2f}"
    return f"{int(round(amount)):,}원"


def us_exchanges(ticker: str) -> Tuple[str, str]:
    """Return (price_excd, trade_excd) for a US ticker; default NASDAQ."""
    trade = _US_EXCHANGE.get(ticker.upper(), "NASD")
    return _TRADE_TO_PRICE_EXCD.get(trade, "NAS"), trade


def name_for(symbol: str, market: str, default: Optional[str] = None) -> str:
    if market == US:
        return US_NAMES.get(symbol.upper(), default if default is not None else symbol.upper())
    # KR names live in names.py to avoid a cycle; imported lazily.
    from .names import name_for as kr_name_for

    return kr_name_for(symbol, default=default)


def symbol_for_name(name: str) -> Optional[Tuple[str, str]]:
    """Map a company name to (symbol, market), trying US then KR."""
    cleaned = name.strip()
    if cleaned in _US_NAME_TO_TICKER:
        return _US_NAME_TO_TICKER[cleaned], US
    from .names import symbol_for as kr_symbol_for

    kr = kr_symbol_for(cleaned)
    return (kr, KR) if kr else None


def prompt_table(limit: int = 12) -> str:
    items = list(US_NAMES.items())[:limit]
    return ", ".join(f"{name}={ticker}" for ticker, name in items)
