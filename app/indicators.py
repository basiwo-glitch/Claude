"""Technical indicators (numpy, vectorised). Values before warm-up are NaN.

Formulas follow the common definitions (Wilder smoothing for RSI/ATR/ADX,
EMA with alpha = 2/(n+1) seeded with the first value, Bollinger with population
std). They are cross-checked against pandas implementations in tests.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.data.base import Candles


def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) == 0:
        return out
    a = 2.0 / (n + 1)
    acc = x[0]
    for i in range(len(x)):
        acc = x[i] if i == 0 else a * x[i] + (1 - a) * acc
        out[i] = acc
    out[: min(n - 1, len(x))] = np.nan
    return out


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x.astype(float), 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def rolling_std(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = w.std(axis=1)
    return out


def rolling_max(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).max(axis=1)
    return out


def rolling_min(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).min(axis=1)
    return out


def rolling_median(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.nanmedian(np.lib.stride_tricks.sliding_window_view(x, n), axis=1)
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's smoothing (RMA), seeded with the SMA of the first n values."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = wilder(up, n)
    ad = wilder(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        r = 100 - 100 / (1 + rs)
    r = np.where(ad == 0, 100.0, r)
    r = np.where((ad == 0) & (au == 0), 50.0, r)
    out[1:] = r
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = np.full(len(close), np.nan)
    valid = ~np.isnan(line)
    if valid.sum() >= signal:
        idx = np.where(valid)[0]
        sig[idx] = ema(line[idx], signal)
    return line, sig, line - sig


def bollinger(close: np.ndarray, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = rolling_std(close, n)
    upper, lower = mid + k * sd, mid - k * sd
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_b = (close - lower) / (upper - lower)
        width = (upper - lower) / mid
    return mid, upper, lower, pct_b, width


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:  # noqa: E741
    prev = np.concatenate([[c[0]], c[:-1]])
    return np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))


def atr(h, l, c, n: int = 14) -> np.ndarray:  # noqa: E741
    return wilder(true_range(h, l, c), n)


def adx(h, l, c, n: int = 14):  # noqa: E741
    """Returns (adx, +DI, -DI)."""
    length = len(c)
    nan = np.full(length, np.nan)
    if length < 2 * n + 1:
        return nan, nan.copy(), nan.copy()
    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(h, l, c)[1:]
    atr_ = wilder(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(pdm, n) / atr_
        ndi = 100 * wilder(ndm, n) / atr_
        dx = 100 * np.abs(pdi - ndi) / (pdi + ndi)
    dx = np.nan_to_num(dx, nan=0.0)
    adx_ = np.full(len(dx), np.nan)
    first = 2 * n - 2
    if len(dx) > first:
        adx_[first] = np.mean(dx[n - 1: first + 1])
        for i in range(first + 1, len(dx)):
            adx_[i] = (adx_[i - 1] * (n - 1) + dx[i]) / n
    return (np.concatenate([[np.nan], adx_]), np.concatenate([[np.nan], pdi]), np.concatenate([[np.nan], ndi]))


def roc(close: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(close), np.nan)
    if len(close) > n:
        out[n:] = (close[n:] / close[:-n] - 1) * 100
    return out


def log_returns(close: np.ndarray) -> np.ndarray:
    out = np.full(len(close), np.nan)
    out[1:] = np.diff(np.log(close))
    return out


@dataclass
class Indicators:
    """All indicator series for one candle set (computed once, shared by strategies)."""

    candles: Candles
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    ema9: np.ndarray
    ema20: np.ndarray
    ema21: np.ndarray
    ema50: np.ndarray
    ema200: np.ndarray
    rsi: np.ndarray
    macd: np.ndarray
    macd_signal: np.ndarray
    macd_hist: np.ndarray
    bb_mid: np.ndarray
    bb_upper: np.ndarray
    bb_lower: np.ndarray
    bb_pctb: np.ndarray
    bb_width: np.ndarray
    bb_width_med50: np.ndarray
    atr: np.ndarray
    atr_pct: np.ndarray
    adx: np.ndarray
    pdi: np.ndarray
    ndi: np.ndarray
    roc12: np.ndarray
    vol_sma20: np.ndarray
    vol_ratio: np.ndarray
    hh20_prev: np.ndarray  # highest high of the 20 bars before the current one
    ll20_prev: np.ndarray
    hh10: np.ndarray
    ll10: np.ndarray
    realized_vol: np.ndarray  # std of log returns over 20 bars

    def __len__(self) -> int:
        return len(self.c)


def compute(c: Candles) -> Indicators:
    o, h, l, cl, v = c.o, c.h, c.l, c.c, c.v  # noqa: E741
    m, ms, mh = macd(cl)
    mid, up, lo, pb, w = bollinger(cl)
    a = atr(h, l, cl)
    ad, pdi, ndi = adx(h, l, cl)
    vs = sma(v, 20)
    vs_prev = np.concatenate([[np.nan], vs[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        vr = v / vs_prev
        atr_pct = a / cl * 100
    hh = rolling_max(h, 20)
    ll = rolling_min(l, 20)
    return Indicators(
        candles=c, o=o, h=h, l=l, c=cl, v=v,
        ema9=ema(cl, 9), ema20=ema(cl, 20), ema21=ema(cl, 21), ema50=ema(cl, 50), ema200=ema(cl, 200),
        rsi=rsi(cl), macd=m, macd_signal=ms, macd_hist=mh,
        bb_mid=mid, bb_upper=up, bb_lower=lo, bb_pctb=pb, bb_width=w, bb_width_med50=rolling_median(w, 50),
        atr=a, atr_pct=atr_pct, adx=ad, pdi=pdi, ndi=ndi, roc12=roc(cl, 12),
        vol_sma20=vs, vol_ratio=vr,
        hh20_prev=np.concatenate([[np.nan], hh[:-1]]), ll20_prev=np.concatenate([[np.nan], ll[:-1]]),
        hh10=rolling_max(h, 10), ll10=rolling_min(l, 10),
        realized_vol=rolling_std(log_returns(cl), 20),
    )
