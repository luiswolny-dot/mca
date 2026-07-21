"""Technical indicators over 1-minute OHLCV.

Design decision — one implementation, two callers
------------------------------------------------
The live scorer and the backtester import *these* functions. Not "the same
formula reimplemented in pandas for research". A large share of strategies that
look good in a notebook and lose money live differ only because the research
indicator used ``df.rolling(...).mean()`` (centred, or including the in-progress
bar) while production used a streaming EMA. Every function here takes a closed
bar array and returns a value aligned to the *last closed* bar.

Design decision — no TA-Lib
---------------------------
TA-Lib is a C dependency that complicates container builds and pins us to its
NaN-padding conventions. These are 20 lines each in numpy and are trivially
unit-testable against hand-computed fixtures.

Array contract: ``ohlcv`` is float64 of shape (n, 6) = [ts, open, high, low,
close, volume], ascending by ts, no gaps filled with NaN.
"""

from __future__ import annotations

import numpy as np

TS, OPEN, HIGH, LOW, CLOSE, VOL = range(6)


def _closes(ohlcv: np.ndarray) -> np.ndarray:
    return np.asarray(ohlcv, dtype=np.float64)[:, CLOSE]


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average, seeded with the first value (streaming-safe).

    Seeding with ``values[0]`` rather than an SMA of the first ``period`` bars
    means the live streaming EMA and the batch EMA converge to identical values
    after ~3*period bars and never disagree about array length.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return values
    alpha = 2.0 / (period + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values: np.ndarray, period: int = 14) -> float | None:
    """Wilder's RSI on the last closed bar. Returns None if insufficient data."""
    values = np.asarray(values, dtype=np.float64)
    if values.size < period + 1:
        return None
    delta = np.diff(values)
    gain = np.clip(delta, 0, None)
    loss = -np.clip(delta, None, 0)

    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    for i in range(period, delta.size):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def macd(values: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[float, float, float] | None:
    """Returns (macd_line, signal_line, histogram) on the last closed bar."""
    values = np.asarray(values, dtype=np.float64)
    if values.size < slow + signal:
        return None
    macd_line = ema(values, fast) - ema(values, slow)
    signal_line = ema(macd_line, signal)
    return float(macd_line[-1]), float(signal_line[-1]), float(macd_line[-1] - signal_line[-1])


def vwap(ohlcv: np.ndarray, lookback: int | None = None) -> float | None:
    """Volume-weighted average price over ``lookback`` bars (all bars if None).

    Uses the typical price (H+L+C)/3 per bar, which is the standard convention
    when true tick data is unavailable.
    """
    arr = np.asarray(ohlcv, dtype=np.float64)
    if arr.size == 0:
        return None
    if lookback is not None:
        arr = arr[-lookback:]
    typical = (arr[:, HIGH] + arr[:, LOW] + arr[:, CLOSE]) / 3.0
    vol = arr[:, VOL]
    total = vol.sum()
    if total <= 0:
        return float(typical[-1])
    return float((typical * vol).sum() / total)


def atr(ohlcv: np.ndarray, period: int = 14) -> float | None:
    """Average True Range (Wilder). Used for volatility-scaled position sizing
    and for the trailing stop distance — a fixed-percent trail is wrong when
    realised vol swings by 10x between tokens."""
    arr = np.asarray(ohlcv, dtype=np.float64)
    if arr.shape[0] < period + 1:
        return None
    high, low, close = arr[:, HIGH], arr[:, LOW], arr[:, CLOSE]
    prev_close = close[:-1]
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)),
    )
    val = tr[:period].mean()
    for i in range(period, tr.size):
        val = (val * (period - 1) + tr[i]) / period
    return float(val)


def realized_volatility(values: np.ndarray, lookback: int = 30) -> float | None:
    """Std-dev of log returns over the lookback, annualised to a 1-day horizon
    (1440 one-minute bars). Meme coins routinely print 300-800% here; the number
    is used for sizing, not for surprise."""
    values = np.asarray(values, dtype=np.float64)
    if values.size < lookback + 1:
        return None
    window = values[-(lookback + 1):]
    if np.any(window <= 0):
        return None
    rets = np.diff(np.log(window))
    return float(rets.std(ddof=1) * np.sqrt(1440))


def order_flow_imbalance(buys: int, sells: int, buy_vol: float, sell_vol: float) -> float:
    """Signed, size-weighted flow imbalance in [-1, 1].

    Counts alone are gameable with dust transactions, so the count imbalance and
    the notional imbalance are averaged. A token with 400 one-dollar buys and
    three $20k sells has positive count imbalance and strongly negative notional
    imbalance; the average correctly reads as distribution.
    """
    n_total = buys + sells
    v_total = buy_vol + sell_vol
    count_imb = (buys - sells) / n_total if n_total > 0 else 0.0
    vol_imb = (buy_vol - sell_vol) / v_total if v_total > 0 else 0.0
    return float(np.clip(0.5 * count_imb + 0.5 * vol_imb, -1.0, 1.0))


def momentum_score(values: np.ndarray) -> float | None:
    """Blend of short-horizon returns, damped so a single wick cannot dominate.

    ``tanh`` compression is intentional: a +900% one-minute candle and a +300%
    one are both "vertical", and treating the first as 3x more attractive is how
    a bot ends up buying the exact top of a pump.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size < 16 or values[-16] <= 0:
        return None
    r5 = values[-1] / values[-6] - 1.0 if values[-6] > 0 else 0.0
    r15 = values[-1] / values[-16] - 1.0
    blended = 0.6 * r5 + 0.4 * r15
    return float(np.tanh(blended * 2.0))
