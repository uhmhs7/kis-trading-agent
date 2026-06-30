"""Small KRX symbol <-> name lookup for friendlier mock output and LLM grounding.

Not exhaustive — covers the default watchlist and well-known large caps so the
demo shows real names instead of MOCK-005930 and the LLM can map a Korean company
name to its 6-digit code.
"""
from __future__ import annotations

from typing import Dict, Optional

SYMBOL_NAMES: Dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "373220": "LG에너지솔루션",
    "207940": "삼성바이오로직스",
    "005380": "현대차",
    "000270": "기아",
    "005490": "POSCO홀딩스",
    "035420": "NAVER",
    "035720": "카카오",
    "051910": "LG화학",
    "006400": "삼성SDI",
    "105560": "KB금융",
    "055550": "신한지주",
    "012330": "현대모비스",
    "028260": "삼성물산",
    "066570": "LG전자",
    "003670": "포스코퓨처엠",
    "096770": "SK이노베이션",
    "017670": "SK텔레콤",
    "015760": "한국전력",
    "034730": "SK",
    "018260": "삼성에스디에스",
    "032830": "삼성생명",
    "000810": "삼성화재",
    "009150": "삼성전기",
    "011200": "HMM",
    "010130": "고려아연",
    "259960": "크래프톤",
    "036570": "엔씨소프트",
    "251270": "넷마블",
    "068270": "셀트리온",
    "247540": "에코프로비엠",
    "086520": "에코프로",
    "066970": "엘앤에프",
    "323410": "카카오뱅크",
}

# Reverse lookup, longest names first so "삼성전자" matches before "삼성".
_NAME_TO_SYMBOL = {name: symbol for symbol, name in SYMBOL_NAMES.items()}


def name_for(symbol: str, default: Optional[str] = None) -> str:
    return SYMBOL_NAMES.get(symbol, default if default is not None else f"종목 {symbol}")


def symbol_for(name: str) -> Optional[str]:
    return _NAME_TO_SYMBOL.get(name.strip())


def prompt_table(limit: int = 16) -> str:
    """A compact 'name=code' list for the LLM system prompt."""
    items = list(SYMBOL_NAMES.items())[:limit]
    return ", ".join(f"{name}={symbol}" for symbol, name in items)
