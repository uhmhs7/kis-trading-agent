from __future__ import annotations

from typing import Iterable, List, Optional


def sma(values: Iterable[float], period: int) -> List[Optional[float]]:
    series = list(values)
    if period <= 0:
        raise ValueError("period must be positive")
    result: List[Optional[float]] = []
    window_sum = 0.0
    for index, value in enumerate(series):
        window_sum += value
        if index >= period:
            window_sum -= series[index - period]
        if index + 1 >= period:
            result.append(window_sum / period)
        else:
            result.append(None)
    return result


def rsi(values: Iterable[float], period: int = 14) -> List[Optional[float]]:
    series = list(values)
    if len(series) < period + 1:
        return [None] * len(series)
    output: List[Optional[float]] = [None] * len(series)
    gains = []
    losses = []
    for index in range(1, period + 1):
        diff = series[index] - series[index - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    output[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(series)):
        diff = series[index] - series[index - 1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        output[index] = _rsi_value(avg_gain, avg_loss)
    return output


def atr(highs: Iterable[float], lows: Iterable[float], closes: Iterable[float], period: int = 14) -> List[Optional[float]]:
    high_series = list(highs)
    low_series = list(lows)
    close_series = list(closes)
    if not (len(high_series) == len(low_series) == len(close_series)):
        raise ValueError("highs, lows, and closes must have the same length")
    true_ranges: List[float] = []
    for index, high in enumerate(high_series):
        low = low_series[index]
        if index == 0:
            true_ranges.append(high - low)
        else:
            previous_close = close_series[index - 1]
            true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sma(true_ranges, period)


def percent_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return ((end - start) / start) * 100


def latest(values: List[Optional[float]]) -> Optional[float]:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

