from datetime import date, timedelta

from trading_agent.config import Settings
from trading_agent.kis_client import KisClient


def test_mock_daily_prices_supports_more_than_100(tmp_path):
    bars = KisClient(Settings(kis_env="mock", data_dir=tmp_path)).daily_prices("005930", days=150)
    assert len(bars) == 150
    dates = [b.date for b in bars]
    assert dates == sorted(dates)  # ascending


# --- Fake session to drive the real (paginated) path ------------------------


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


def _rows(end_date, n):
    rows = []
    day = end_date
    for _ in range(n):
        rows.append(
            {
                "stck_bsop_date": day.strftime("%Y%m%d"),
                "stck_oprc": "100",
                "stck_hgpr": "110",
                "stck_lwpr": "90",
                "stck_clpr": "105",
                "acml_vol": "1000",
            }
        )
        day -= timedelta(days=1)
    return rows


class FakeSession:
    def __init__(self, pages):
        self.pages = list(pages)
        self.daily_calls = 0

    def post(self, url, headers=None, data=None, timeout=None):
        return _Resp(200, {"access_token": "tok", "access_token_token_expired": "2999-01-01 00:00:00"})

    def request(self, method=None, url=None, headers=None, params=None, data=None, timeout=None):
        page = self.pages[self.daily_calls] if self.daily_calls < len(self.pages) else []
        self.daily_calls += 1
        return _Resp(200, {"rt_cd": "0", "output2": page})


def test_real_daily_prices_paginates_across_pages(tmp_path):
    page1 = _rows(date(2026, 6, 1), 100)
    oldest1 = date(2026, 6, 1) - timedelta(days=99)
    page2 = _rows(oldest1 - timedelta(days=1), 100)
    settings = Settings(
        kis_env="paper",
        data_dir=tmp_path,
        paper_app_key="k",
        paper_app_secret="s",
        paper_account_no="12345678",
        token_cache_dir=tmp_path,
    )
    client = KisClient(settings, session=FakeSession([page1, page2]))
    bars = client.daily_prices("005930", days=150)
    assert len(bars) == 150  # stitched from two 100-row pages
    assert bars[-1].date == "20260601"  # most recent kept
    dates = [b.date for b in bars]
    assert dates == sorted(dates)
