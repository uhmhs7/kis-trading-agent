import pytest

from trading_agent.indicators import atr, latest, percent_change, rsi, sma


def test_sma_and_latest():
    values = [1, 2, 3, 4, 5]
    assert sma(values, 3) == [None, None, 2.0, 3.0, 4.0]
    assert latest([None, None, 4.0]) == 4.0


def test_sma_period_validation():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


def test_rsi_bounds():
    values = list(range(1, 30))
    result = rsi(values, 14)
    assert result[-1] == 100.0


def test_rsi_short_series_all_none():
    assert rsi([1, 2, 3], 14) == [None, None, None]


def test_atr_basic():
    highs = [10, 12, 11]
    lows = [8, 9, 9]
    closes = [9, 11, 10]
    # TR: [10-8=2, max(12-9,|12-9|,|9-9|)=3, max(11-9,|11-11|,|9-11|)=2]
    out = atr(highs, lows, closes, 2)
    assert out[0] is None
    assert out[1] == 2.5
    assert out[2] == 2.5


def test_atr_length_mismatch_raises():
    with pytest.raises(ValueError):
        atr([1, 2], [1], [1, 2], 2)


def test_percent_change():
    assert percent_change(100, 110) == 10
    assert percent_change(110, 99) == -10
    assert percent_change(0, 110) == 0
