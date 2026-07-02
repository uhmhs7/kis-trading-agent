from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

from . import markets
from .config import Settings


class KisAPIError(RuntimeError):
    def __init__(self, message: str, status_code: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    volume: int
    raw: Dict[str, Any]
    market: str = "KR"
    currency: str = "KRW"


def _is_rate_limited(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = str(payload.get("msg_cd") or payload.get("error_code") or "")
    msg = str(payload.get("msg1") or "")
    return code == "EGW00201" or "초당" in msg or "거래건수" in msg


def _fmt_price(price: float, market: str) -> str:
    """Format an order price for KIS: integer KRW, decimal USD."""
    if market == markets.US:
        return f"{float(price):.4f}".rstrip("0").rstrip(".")
    return str(int(round(float(price))))


@dataclass
class PriceBar:
    date: str
    open: int
    high: int
    low: int
    close: int
    volume: int


def _to_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return default


class KisClient:
    """Small KIS Open API REST client for the agent's required domestic-stock calls."""

    TOKEN_PATH = "/oauth2/tokenP"
    QUOTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
    DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
    ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
    # Overseas (US) endpoints. TR IDs/params follow the KIS overseas spec; verify
    # against the current portal before live use (the demo runs on mock).
    OVERSEAS_QUOTE_PATH = "/uapi/overseas-price/v1/quotations/price"
    OVERSEAS_DAILY_PATH = "/uapi/overseas-price/v1/quotations/dailyprice"
    OVERSEAS_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
    OVERSEAS_PRESENT_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-present-balance"
    OVERSEAS_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
    HASHKEY_PATH = "/uapi/hashkey"
    MIN_REQUEST_INTERVAL = 0.6  # KIS 모의투자(VTS)는 초당 호출이 빡빡 → 여유있게 간격

    def __init__(self, settings: Settings, session: Optional[requests.Session] = None):
        self.settings = settings
        self.session = session or requests.Session()
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        """Coarse global rate limit for real KIS calls (no-op in mock)."""
        if self.settings.is_mock:
            return
        with self._throttle_lock:
            wait = self.MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def quote(self, symbol: str, market: str = markets.KR) -> Quote:
        if self.settings.is_mock:
            return self._mock_quote(symbol, market)
        if market == markets.US:
            return self._overseas_quote(symbol)
        payload = self._request(
            "GET",
            self.QUOTE_PATH,
            "FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        output = payload.get("output") or {}
        return Quote(
            symbol=symbol,
            name=output.get("hts_kor_isnm") or markets.name_for(symbol, markets.KR, default=symbol),
            price=_to_int(output.get("stck_prpr")),
            change=_to_int(output.get("prdy_vrss")),
            change_pct=_to_float(output.get("prdy_ctrt")),
            open=_to_int(output.get("stck_oprc")),
            high=_to_int(output.get("stck_hgpr")),
            low=_to_int(output.get("stck_lwpr")),
            volume=_to_int(output.get("acml_vol")),
            raw=output,
            market=markets.KR,
            currency="KRW",
        )

    def _overseas_quote(self, symbol: str) -> Quote:
        price_excd, _ = markets.us_exchanges(symbol)
        payload = self._request(
            "GET",
            self.OVERSEAS_QUOTE_PATH,
            "HHDFS00000300",
            params={"AUTH": "", "EXCD": price_excd, "SYMB": symbol},
        )
        output = payload.get("output") or {}
        # KIS overseas 'diff' is often unsigned; the signed 'rate' (%) carries the
        # direction — align the change sign to it so up/down never contradict.
        change = _to_float(output.get("diff"))
        rate = _to_float(output.get("rate"))
        if rate < 0:
            change = -abs(change)
        elif rate > 0:
            change = abs(change)
        return Quote(
            symbol=symbol,
            name=markets.name_for(symbol, markets.US, default=symbol),
            price=_to_float(output.get("last")),
            change=change,
            change_pct=rate,
            open=_to_float(output.get("open")),
            high=_to_float(output.get("high")),
            low=_to_float(output.get("low")),
            volume=_to_int(output.get("tvol")),
            raw=output,
            market=markets.US,
            currency="USD",
        )

    def daily_prices(self, symbol: str, market: str = markets.KR, days: int = 100) -> List[PriceBar]:
        if self.settings.is_mock:
            return self._mock_daily_prices(symbol, market, days=days)
        if market == markets.US:
            return self._overseas_daily(symbol, days=days)
        return self._domestic_daily(symbol, days=days)

    def _domestic_daily(self, symbol: str, days: int = 100) -> List[PriceBar]:
        # inquire-daily-itemchartprice returns at most ~100 rows per call, so page
        # backward through date windows until we have `days` bars (or run out).
        collected: Dict[str, PriceBar] = {}
        end = date.today()
        for _ in range(max(1, days // 90 + 2)):
            start = end - timedelta(days=150)
            payload = self._request(
                "GET",
                self.DAILY_PATH,
                "FHKST03010100",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
            )
            rows = payload.get("output2") or []
            if not rows:
                break
            added = 0
            oldest: Optional[str] = None
            for row in rows:
                day = str(row.get("stck_bsop_date", ""))
                if not day:
                    continue
                if oldest is None or day < oldest:
                    oldest = day
                if day in collected:
                    continue
                bar = PriceBar(
                    date=day,
                    open=_to_int(row.get("stck_oprc")),
                    high=_to_int(row.get("stck_hgpr")),
                    low=_to_int(row.get("stck_lwpr")),
                    close=_to_int(row.get("stck_clpr")),
                    volume=_to_int(row.get("acml_vol")),
                )
                if bar.close > 0:
                    collected[day] = bar
                    added += 1
            if len(collected) >= days or oldest is None or added == 0:
                break
            next_end = datetime.strptime(oldest, "%Y%m%d").date() - timedelta(days=1)
            if next_end >= end:  # no backward progress — stop to avoid a loop
                break
            end = next_end
        return sorted(collected.values(), key=lambda item: item.date)[-days:]

    def _overseas_daily(self, symbol: str, days: int = 100) -> List[PriceBar]:
        # dailyprice returns up to ~100 rows ending at BYMD; page backward by BYMD.
        price_excd, _ = markets.us_exchanges(symbol)
        collected: Dict[str, PriceBar] = {}
        bymd = ""  # empty = most recent
        for _ in range(max(1, days // 90 + 2)):
            payload = self._request(
                "GET",
                self.OVERSEAS_DAILY_PATH,
                "HHDFS76240000",
                params={
                    "AUTH": "",
                    "EXCD": price_excd,
                    "SYMB": symbol,
                    "GUBN": "0",  # 0=일, 1=주, 2=월
                    "BYMD": bymd,
                    "MODP": "1",  # 수정주가 반영
                },
            )
            rows = payload.get("output2") or []
            if not rows:
                break
            added = 0
            oldest: Optional[str] = None
            for row in rows:
                day = str(row.get("xymd", ""))
                if not day:
                    continue
                if oldest is None or day < oldest:
                    oldest = day
                if day in collected:
                    continue
                bar = PriceBar(
                    date=day,
                    open=_to_float(row.get("open")),
                    high=_to_float(row.get("high")),
                    low=_to_float(row.get("low")),
                    close=_to_float(row.get("clos")),
                    volume=_to_int(row.get("tvol")),
                )
                if bar.close > 0:
                    collected[day] = bar
                    added += 1
            if len(collected) >= days or oldest is None or added == 0:
                break
            next_bymd = (datetime.strptime(oldest, "%Y%m%d").date() - timedelta(days=1)).strftime("%Y%m%d")
            if next_bymd == bymd:
                break
            bymd = next_bymd
        return sorted(collected.values(), key=lambda item: item.date)[-days:]

    def balance(self, market: str = markets.KR) -> Dict[str, Any]:
        if self.settings.is_mock:
            base = self.settings.base_equity_usd if market == markets.US else self.settings.base_equity_krw
            return {"positions": [], "summary": [{"tot_evlu_amt": str(base)}], "raw": {"mode": "mock"}}
        self.settings.validate_runtime()
        if not self.settings.has_account:
            raise KisAPIError("Account number is missing for balance lookup.")
        if market == markets.US:
            return self._overseas_balance()
        tr_id = "VTTC8434R" if self.settings.is_paper else "TTTC8434R"
        payload = self._request(
            "GET",
            self.BALANCE_PATH,
            tr_id,
            params={
                "CANO": self.settings.active_account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        return {
            "positions": payload.get("output1") or [],
            "summary": payload.get("output2") or [],
            "raw": payload,
        }

    def _overseas_balance(self) -> Dict[str, Any]:
        tr_id = "VTTS3012R" if self.settings.is_paper else "TTTS3012R"
        payload = self._request(
            "GET",
            self.OVERSEAS_BALANCE_PATH,
            tr_id,
            params={
                "CANO": self.settings.active_account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        result = {
            "positions": payload.get("output1") or [],
            "summary": payload.get("output2") or [],
            "raw": payload,
        }
        # The overseas inquire-balance has no cash/total-asset field — fetch the
        # present-balance (체결기준현재잔고) for foreign-currency deposit best-effort.
        try:
            result["present"] = self._overseas_present_balance()
        except Exception:  # pragma: no cover - best effort
            result["present"] = {}
        return result

    def _overseas_present_balance(self) -> Dict[str, Any]:
        tr_id = "VTRP6504R" if self.settings.is_paper else "CTRP6504R"
        return self._request(
            "GET",
            self.OVERSEAS_PRESENT_BALANCE_PATH,
            tr_id,
            params={
                "CANO": self.settings.active_account_no,
                "ACNT_PRDT_CD": self.settings.account_product_code,
                "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "000",
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
            },
        )

    def cash_order(
        self,
        side: str,
        symbol: str,
        quantity: int,
        limit_price: float,
        order_type: str = "00",
        market: str = markets.KR,
    ) -> Dict[str, Any]:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell.")
        if self.settings.is_mock:
            return self._mock_order(side, symbol, quantity, limit_price, order_type, market)
        self.settings.validate_runtime()
        if not self.settings.has_account:
            raise KisAPIError("Account number is missing for order placement.")
        if market == markets.US:
            return self._overseas_order(side, symbol, quantity, limit_price)
        if self.settings.is_paper:
            tr_id = "VTTC0012U" if side == "buy" else "VTTC0011U"
        else:
            tr_id = "TTTC0012U" if side == "buy" else "TTTC0011U"
        body = {
            "CANO": self.settings.active_account_no,
            "ACNT_PRDT_CD": self.settings.account_product_code,
            "PDNO": symbol,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": _fmt_price(limit_price, markets.KR),
            "EXCG_ID_DVSN_CD": "KRX",
            "SLL_TYPE": "01" if side == "sell" else "",
            "CNDT_PRIC": "",
        }
        return self._request("POST", self.ORDER_CASH_PATH, tr_id, body=body)

    def _overseas_order(self, side: str, symbol: str, quantity: int, limit_price: float) -> Dict[str, Any]:
        _, trade_excd = markets.us_exchanges(symbol)
        if self.settings.is_paper:
            tr_id = "VTTT1002U" if side == "buy" else "VTTT1001U"
        else:
            tr_id = "TTTT1002U" if side == "buy" else "TTTT1006U"
        body = {
            "CANO": self.settings.active_account_no,
            "ACNT_PRDT_CD": self.settings.account_product_code,
            "OVRS_EXCG_CD": trade_excd,
            "PDNO": symbol,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": _fmt_price(limit_price, markets.US),
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00",  # limit order
        }
        return self._request("POST", self.OVERSEAS_ORDER_PATH, tr_id, body=body)

    def _request(
        self,
        method: str,
        path: str,
        tr_id: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        token = self._access_token()
        headers = {
            "content-type": "application/json",
            "accept": "text/plain",
            "authorization": f"Bearer {token}",
            "appkey": self.settings.active_app_key,
            "appsecret": self.settings.active_app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": "",
            "User-Agent": self.settings.user_agent,
        }
        if method.upper() == "POST" and body:
            hashkey = self._hashkey(body, headers)
            if hashkey:
                headers["hashkey"] = hashkey

        # KIS per-second rate limits (esp. 모의투자) are transient — retry with backoff.
        last_exc = None
        for attempt in range(4):
            self._throttle()
            response = self.session.request(
                method=method.upper(),
                url=f"{self.settings.base_url}{path}",
                headers=headers,
                params=params,
                data=json.dumps(body) if body is not None else None,
                timeout=15,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise KisAPIError(
                    f"KIS API returned non-JSON response: {response.text[:200]}",
                    response.status_code,
                ) from exc
            ok = response.status_code == 200 and payload.get("rt_cd") in (None, "0")
            if ok:
                return payload
            msg = payload.get("msg1") or response.text or "KIS API error"
            last_exc = KisAPIError(msg, response.status_code, payload)
            if _is_rate_limited(payload) and attempt < 3:
                time.sleep(0.7 * (attempt + 1))
                continue
            raise last_exc
        raise last_exc  # pragma: no cover

    def _access_token(self) -> str:
        self.settings.validate_runtime()
        token_path = self._token_cache_path()
        cached = self._read_cached_token(token_path)
        if cached:
            return cached
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.settings.active_app_key,
            "appsecret": self.settings.active_app_secret,
        }
        self._throttle()  # space token issuance from the call that follows it
        response = self.session.post(
            f"{self.settings.base_url}{self.TOKEN_PATH}",
            headers={
                "content-type": "application/json",
                "User-Agent": self.settings.user_agent,
            },
            data=json.dumps(payload),
            timeout=15,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise KisAPIError(
                f"Token endpoint returned non-JSON response: {response.text[:200]}",
                response.status_code,
            ) from exc
        if response.status_code != 200 or "access_token" not in data:
            raise KisAPIError(
                data.get("msg1", "Failed to issue KIS access token."),
                response.status_code,
                data,
            )
        expires_at = self._parse_expiry(data)
        # The token grants trading access for its lifetime — keep the cache 0o600.
        token_path.write_text(
            json.dumps({"access_token": data["access_token"], "expires_at": expires_at}),
            encoding="utf-8",
        )
        try:
            os.chmod(token_path, 0o600)
        except OSError:  # pragma: no cover - best effort on exotic filesystems
            pass
        return data["access_token"]

    def _hashkey(self, body: Dict[str, Any], base_headers: Dict[str, str]) -> str:
        headers = {
            "content-type": "application/json",
            "appkey": self.settings.active_app_key,
            "appsecret": self.settings.active_app_secret,
            "authorization": base_headers["authorization"],
            "User-Agent": self.settings.user_agent,
        }
        response = self.session.post(
            f"{self.settings.base_url}{self.HASHKEY_PATH}",
            headers=headers,
            data=json.dumps(body),
            timeout=15,
        )
        if response.status_code != 200:
            return ""
        return response.json().get("HASH", "")

    def _token_cache_path(self) -> Path:
        suffix = "paper" if self.settings.is_paper else "prod"
        digest = hashlib.sha256(self.settings.active_app_key.encode("utf-8")).hexdigest()[:10]
        return self.settings.token_cache_dir / f"kis_token_{suffix}_{digest}.json"

    def _read_cached_token(self, path: Path) -> Optional[str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if data.get("expires_at", 0) - time.time() <= 300:
            return None
        return data.get("access_token")

    @staticmethod
    def _parse_expiry(payload: Dict[str, Any]) -> float:
        raw = payload.get("access_token_token_expired")
        if raw:
            try:
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                pass
        return time.time() + int(payload.get("expires_in", 86_400))

    def _mock_daily_prices(self, symbol: str, market: str = markets.KR, days: int = 100) -> List[PriceBar]:
        seed = int(hashlib.sha256(f"{market}:{symbol}".encode("utf-8")).hexdigest()[:12], 16)
        rng = random.Random(seed)
        if market == markets.US:
            base = 20.0 + seed % 480  # ~$20–$500
            floor = 1.0
            is_us = True
        else:
            base = 20_000 + seed % 90_000
            floor = 1_000.0
            is_us = False
        drift = rng.uniform(-0.0004, 0.0011)
        price = float(base)
        bars: List[PriceBar] = []
        current = date.today()
        while len(bars) < max(days, 100):
            if current.weekday() < 5:
                open_price = max(floor, price * (1 + rng.gauss(0, 0.006)))
                close = max(floor, open_price * (1 + rng.gauss(drift, 0.018)))
                high = max(open_price, close) * (1 + abs(rng.gauss(0.004, 0.006)))
                low = min(open_price, close) * (1 - abs(rng.gauss(0.004, 0.006)))
                if is_us:
                    o, h, low_, c = (round(v, 2) for v in (open_price, high, low, close))
                    volume = int(500_000 + rng.random() * 40_000_000)
                else:
                    o, h, low_, c = (int(round(v)) for v in (open_price, high, low, close))
                    volume = int(200_000 + rng.random() * 4_500_000)
                bars.append(PriceBar(date=current.strftime("%Y%m%d"), open=o, high=h, low=low_, close=c, volume=volume))
                price = close
            current -= timedelta(days=1)
        return sorted(bars, key=lambda item: item.date)[-days:]

    def _mock_quote(self, symbol: str, market: str = markets.KR) -> Quote:
        bars = self._mock_daily_prices(symbol, market, days=2)
        previous, latest = bars[-2], bars[-1]
        change = round(latest.close - previous.close, 2)
        change_pct = round((change / previous.close) * 100, 2) if previous.close else 0.0
        return Quote(
            symbol=symbol,
            name=markets.name_for(symbol, market, default=symbol),
            price=latest.close,
            change=change,
            change_pct=change_pct,
            open=latest.open,
            high=latest.high,
            low=latest.low,
            volume=latest.volume,
            raw={"mode": "mock", "date": latest.date},
            market=market,
            currency=markets.currency_of(market),
        )

    def _mock_order(
        self, side: str, symbol: str, quantity: int, limit_price: float, order_type: str, market: str = markets.KR
    ) -> Dict[str, Any]:
        order_no = hashlib.sha256(
            f"{side}:{symbol}:{quantity}:{limit_price}:{time.time()}".encode("utf-8")
        ).hexdigest()[:12].upper()
        return {
            "rt_cd": "0",
            "msg1": "mock order accepted",
            "output": {
                "KRX_FWDG_ORD_ORGNO": "MOCK",
                "ODNO": order_no,
                "ORD_TMD": datetime.now().strftime("%H%M%S"),
                "PDNO": symbol,
                "ORD_QTY": str(quantity),
                "ORD_UNPR": _fmt_price(limit_price, market),
                "ORD_DVSN": order_type,
                "SLL_BUY_DVSN_CD": "02" if side == "buy" else "01",
            },
        }


def quote_to_dict(quote: Quote) -> Dict[str, Any]:
    return asdict(quote)


def bars_to_dicts(bars: Iterable[PriceBar]) -> List[Dict[str, Any]]:
    return [asdict(bar) for bar in bars]
