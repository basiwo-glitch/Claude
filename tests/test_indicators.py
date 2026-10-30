"""Indicators cross-checked against independent pandas implementations."""
import numpy as np
import pandas as pd

from app import indicators as I
from app.data.base import Candles, resample
from app.patterns import support_resistance, market_structure


def series(n=400, seed=3):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.004, n))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.004, n))
    v = rng.uniform(100, 1000, n)
    return o, h, l, c, v


def wilder_pd(x: pd.Series, n: int) -> pd.Series:
    out = pd.Series(np.nan, index=x.index)
    out.iloc[n - 1] = x.iloc[:n].mean()
    for i in range(n, len(x)):
        out.iloc[i] = (out.iloc[i - 1] * (n - 1) + x.iloc[i]) / n
    return out


def test_ema_sma_match_pandas():
    *_, c, _ = series()
    s = pd.Series(c)
    np.testing.assert_allclose(I.ema(c, 20)[19:], s.ewm(span=20, adjust=False).mean().values[19:], rtol=1e-12)
    np.testing.assert_allclose(I.sma(c, 20)[19:], s.rolling(20).mean().values[19:], rtol=1e-12)
    assert np.isnan(I.ema(c, 20)[:19]).all()


def test_rsi_matches_wilder_definition():
    *_, c, _ = series()
    d = pd.Series(c).diff()
    au = wilder_pd(d.clip(lower=0).iloc[1:].reset_index(drop=True), 14)
    ad = wilder_pd((-d.clip(upper=0)).iloc[1:].reset_index(drop=True), 14)
    ref = 100 - 100 / (1 + au / ad)
    got = I.rsi(c, 14)[1:]
    np.testing.assert_allclose(got[13:], ref.values[13:], rtol=1e-10)
    assert ((got[13:] >= 0) & (got[13:] <= 100)).all()


def test_atr_bollinger_macd():
    o, h, l, c, v = series()
    prev = pd.Series(c).shift(1).fillna(c[0])
    tr = pd.concat([pd.Series(h - l), (pd.Series(h) - prev).abs(), (pd.Series(l) - prev).abs()], axis=1).max(axis=1)
    np.testing.assert_allclose(I.atr(h, l, c, 14)[13:], wilder_pd(tr, 14).values[13:], rtol=1e-10)
    mid, up, lo, pb, w = I.bollinger(c, 20, 2)
    s = pd.Series(c)
    np.testing.assert_allclose(up[19:], (s.rolling(20).mean() + 2 * s.rolling(20).std(ddof=0)).values[19:], rtol=1e-10)
    line, sig, hist = I.macd(c)
    ref_line = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    np.testing.assert_allclose(line[25:], ref_line.values[25:], rtol=1e-10)
    np.testing.assert_allclose(hist[40:], (line - sig)[40:])


def test_adx_range_and_trend_detection():
    n = 300
    c = np.linspace(100, 200, n)  # perfect uptrend
    h, l = c * 1.002, c * 0.998
    adx, pdi, ndi = I.adx(h, l, c, 14)
    assert np.nanmin(adx[60:]) > 50 and (pdi[60:] > ndi[60:]).all()


def test_resample_aggregates_correctly():
    t = np.arange(0, 3600 * 3, 60, dtype=float)
    c = np.arange(len(t), dtype=float) + 1
    cd = Candles("X", "1m", t, c - 0.5, c + 1, c - 1, c, np.ones(len(t)))
    r = resample(cd, "1h")
    assert len(r) == 3
    assert r.o[0] == 0.5 and r.c[0] == 60 and r.h[0] == 61 and r.l[0] == 0 and r.v[0] == 60
    assert r.t[1] == 3600


def test_support_resistance_and_structure():
    o, h, l, c, v = series(500, 9)
    ind = I.compute(Candles("X", "1h", np.arange(500) * 3600.0, o, h, l, c, v))
    sr = support_resistance(ind)
    if sr["nearest_support"]:
        assert sr["nearest_support"] < c[-1]
    if sr["nearest_resistance"]:
        assert sr["nearest_resistance"] > c[-1]
    assert market_structure(ind)["structure"] in ("aufwärts", "abwärts", "seitwärts", "unklar")
