"""Candlestick patterns, support/resistance zones and market structure."""
from __future__ import annotations

import numpy as np

from app.indicators import Indicators


def candlestick_patterns(ind: Indicators, i: int | None = None) -> list[dict]:
    """Classic patterns on bar i (default: last bar). Each entry: name, bias, description."""
    o, h, l, c = ind.o, ind.h, ind.l, ind.c  # noqa: E741
    n = len(c)
    i = n - 1 if i is None else i
    if i < 2:
        return []
    out: list[dict] = []
    body = abs(c[i] - o[i])
    rng = max(h[i] - l[i], 1e-12)
    upper = h[i] - max(c[i], o[i])
    lower = min(c[i], o[i]) - l[i]
    pbody = abs(c[i - 1] - o[i - 1])
    avg_body = float(np.mean(np.abs(c[max(0, i - 14):i] - o[max(0, i - 14):i]))) or 1e-12

    if body / rng < 0.1:
        out.append({"name": "Doji", "bias": "neutral", "desc": "Unentschlossenheit (Körper < 10 % der Spanne)"})
    if lower >= 2 * body and upper <= 0.3 * rng and body > 0:
        if c[i - 1] < c[i - 2]:
            out.append({"name": "Hammer", "bias": "bullish", "desc": "Langer unterer Docht nach Abwärtsbewegung"})
    if upper >= 2 * body and lower <= 0.3 * rng and body > 0:
        if c[i - 1] > c[i - 2]:
            out.append({"name": "Shooting Star", "bias": "bearish", "desc": "Langer oberer Docht nach Aufwärtsbewegung"})
    if c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1] and body > pbody:
        out.append({"name": "Bullish Engulfing", "bias": "bullish", "desc": "Grüne Kerze umschließt vorherige rote"})
    if c[i] < o[i] and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1] and body > pbody:
        out.append({"name": "Bearish Engulfing", "bias": "bearish", "desc": "Rote Kerze umschließt vorherige grüne"})
    if h[i] <= h[i - 1] and l[i] >= l[i - 1]:
        out.append({"name": "Inside Bar", "bias": "neutral", "desc": "Spanne innerhalb der Vorkerze (Kompression)"})
    # three-bar patterns
    b2 = abs(c[i - 2] - o[i - 2])
    b1 = abs(c[i - 1] - o[i - 1])
    if c[i - 2] < o[i - 2] and b2 > avg_body and b1 < 0.5 * avg_body and c[i] > o[i] and c[i] > (o[i - 2] + c[i - 2]) / 2:
        out.append({"name": "Morning Star", "bias": "bullish", "desc": "Dreikerzen-Umkehr nach unten-Trend"})
    if c[i - 2] > o[i - 2] and b2 > avg_body and b1 < 0.5 * avg_body and c[i] < o[i] and c[i] < (o[i - 2] + c[i - 2]) / 2:
        out.append({"name": "Evening Star", "bias": "bearish", "desc": "Dreikerzen-Umkehr nach Aufwärtstrend"})
    if body > 2 * avg_body:
        out.append({"name": "Wide Range Bar", "bias": "bullish" if c[i] > o[i] else "bearish",
                    "desc": f"Körper {body / avg_body:.1f}x größer als Durchschnitt"})
    return out


def swing_points(h: np.ndarray, l: np.ndarray, k: int = 3) -> tuple[list[int], list[int]]:  # noqa: E741
    """Fractal swing highs/lows: bar is the extreme of the k bars on each side."""
    highs, lows = [], []
    for i in range(k, len(h) - k):
        if h[i] == h[i - k: i + k + 1].max():
            highs.append(i)
        if l[i] == l[i - k: i + k + 1].min():
            lows.append(i)
    return highs, lows


def support_resistance(ind: Indicators, lookback: int = 300, k: int = 3, max_zones: int = 4) -> dict:
    """Cluster swing points into zones (width 0.5 ATR); returns nearest supports/resistances."""
    n = len(ind.c)
    a0 = max(0, n - lookback)
    h, l = ind.h[a0:], ind.l[a0:]  # noqa: E741
    price = float(ind.c[-1])
    atr_ = float(ind.atr[-1]) if not np.isnan(ind.atr[-1]) else price * 0.01
    sh, sl = swing_points(h, l, k)
    levels = sorted([float(h[i]) for i in sh] + [float(l[i]) for i in sl])
    zones: list[dict] = []
    for lv in levels:
        if zones and lv - zones[-1]["high"] <= 0.5 * atr_:
            z = zones[-1]
            z["high"] = max(z["high"], lv)
            z["low"] = min(z["low"], lv)
            z["touches"] += 1
        else:
            zones.append({"low": lv, "high": lv, "touches": 1})
    for z in zones:
        z["mid"] = (z["low"] + z["high"]) / 2
    sup = sorted([z for z in zones if z["mid"] < price], key=lambda z: -z["mid"])[:max_zones]
    res = sorted([z for z in zones if z["mid"] > price], key=lambda z: z["mid"])[:max_zones]
    return {
        "supports": [{k2: round(v, 8) if isinstance(v, float) else v for k2, v in z.items()} for z in sup],
        "resistances": [{k2: round(v, 8) if isinstance(v, float) else v for k2, v in z.items()} for z in res],
        "nearest_support": sup[0]["mid"] if sup else None,
        "nearest_resistance": res[0]["mid"] if res else None,
    }


def market_structure(ind: Indicators, k: int = 3) -> dict:
    """Higher highs / higher lows = uptrend, lower highs / lower lows = downtrend."""
    sh, sl = swing_points(ind.h, ind.l, k)
    if len(sh) < 2 or len(sl) < 2:
        return {"structure": "unklar", "detail": "zu wenige Swingpunkte"}
    h1, h2 = ind.h[sh[-2]], ind.h[sh[-1]]
    l1, l2 = ind.l[sl[-2]], ind.l[sl[-1]]
    hh, hl = h2 > h1, l2 > l1
    if hh and hl:
        s = "aufwärts"
    elif not hh and not hl:
        s = "abwärts"
    else:
        s = "seitwärts"
    return {"structure": s, "detail": f"{'HH' if hh else 'LH'}/{'HL' if hl else 'LL'}",
            "last_swing_high": float(h2), "last_swing_low": float(l2)}


def trend_label(ind: Indicators, i: int = -1) -> str:
    c, e20, e50, e200 = ind.c[i], ind.ema20[i], ind.ema50[i], ind.ema200[i]
    if np.isnan(e50):
        return "n/a"
    if not np.isnan(e200):
        if c > e20 > e50 > e200:
            return "up"
        if c < e20 < e50 < e200:
            return "down"
    if c > e50 and e20 > e50:
        return "up"
    if c < e50 and e20 < e50:
        return "down"
    return "flat"
