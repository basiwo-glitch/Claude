"""Strategy framework.

A strategy is a set of *vectorised* rule conditions on closed candles plus a rule
for stop-loss / take-profit. Because rules are vectorised, the exact same rule
can be evaluated on the whole history of a market — that is how the Strategy
Analyst measures (instead of inventing) hit rates and expected values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from app.indicators import Indicators


@dataclass
class Condition:
    key: str
    label: str  # human readable (German)
    long: np.ndarray
    short: np.ndarray
    value: np.ndarray | None = None
    fmt: str = "{:.2f}"


@dataclass
class HistoryStats:
    """Outcome of replaying the rule on past bars of the same market (non-overlapping trades)."""

    n: int = 0
    wins: int = 0
    p: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    mean_r: float | None = None  # net of costs
    median_r: float | None = None
    profit_factor: float | None = None
    tp_hits: int = 0
    sl_hits: int = 0
    time_exits: int = 0
    bars_used: int = 0
    r_values: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "r_values"}
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d


def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


class Strategy:
    name: str = "base"
    label: str = "Base"
    description: str = ""
    timeframes: dict[str, str] = {"crypto": "1h", "stock": "1h"}
    max_hold_bars: int = 24
    sl_atr: float = 1.5
    tp_atr: float = 3.0
    min_rr: float | None = None  # None -> global MIN_REWARD_RISK
    warmup: int = 210

    def timeframe(self, asset_class: str) -> str:
        return self.timeframes.get(asset_class, next(iter(self.timeframes.values())))

    def conditions(self, ind: Indicators) -> list[Condition]:  # pragma: no cover - abstract
        raise NotImplementedError

    def entries(self, ind: Indicators, conds: list[Condition] | None = None) -> tuple[np.ndarray, np.ndarray]:
        conds = conds if conds is not None else self.conditions(ind)
        n = len(ind)
        lo = np.ones(n, dtype=bool)
        sh = np.ones(n, dtype=bool)
        for cd in conds:
            lo &= np.nan_to_num(cd.long, nan=0).astype(bool)
            sh &= np.nan_to_num(cd.short, nan=0).astype(bool)
        lo[: self.warmup] = False
        sh[: self.warmup] = False
        return lo, sh

    def levels(self, ind: Indicators, side: str) -> tuple[np.ndarray, np.ndarray]:
        a = ind.atr
        if side == "long":
            return ind.c - self.sl_atr * a, ind.c + self.tp_atr * a
        return ind.c + self.sl_atr * a, ind.c - self.tp_atr * a

    # ------------------------------------------------------------------------
    def explain(self, ind: Indicators, i: int, side: str, conds: list[Condition] | None = None) -> list[dict]:
        conds = conds if conds is not None else self.conditions(ind)
        out = []
        for cd in conds:
            arr = cd.long if side == "long" else cd.short
            met = bool(np.nan_to_num(arr[i], nan=0))
            val = None
            if cd.value is not None and not (isinstance(cd.value[i], float) and math.isnan(cd.value[i])):
                try:
                    val = cd.fmt.format(float(cd.value[i]))
                except (ValueError, TypeError):
                    val = None
            out.append({"key": cd.key, "label": cd.label, "met": met, "value": val})
        return out

    def replay(self, ind: Indicators, side: str, cost_frac: float, entries: np.ndarray | None = None,
               end: int | None = None) -> HistoryStats:
        """Replay the rule over history. cost_frac = round-trip costs as fraction of price.

        Entry at the signal bar's close, exits checked on subsequent bars; when stop and
        target are inside the same bar the stop is assumed first (conservative). Gaps
        through the stop fill at the bar's open. Trades that are not resolved before
        ``end`` are ignored (no look-ahead).
        """
        o, h, l, c = ind.o, ind.h, ind.l, ind.c  # noqa: E741
        n = len(c) if end is None else end
        if entries is None:
            lo, sh = self.entries(ind)
            entries = lo if side == "long" else sh
        sl_arr, tp_arr = self.levels(ind, side)
        st = HistoryStats(bars_used=n)
        busy_until = -1
        rs: list[float] = []
        for i in np.nonzero(entries[:n])[0]:
            if i <= busy_until or i >= n - 1:
                continue
            e, s, t = c[i], sl_arr[i], tp_arr[i]
            risk = abs(e - s)
            if not np.isfinite(risk) or risk <= 0 or not np.isfinite(t):
                continue
            r = None
            exit_j = None
            last = min(i + self.max_hold_bars, n - 1)
            for j in range(i + 1, last + 1):
                if side == "long":
                    if l[j] <= s:
                        r = (min(s, o[j]) - e) / risk
                        st.sl_hits += 1
                    elif h[j] >= t:
                        r = (t - e) / risk
                        st.tp_hits += 1
                else:
                    if h[j] >= s:
                        r = (e - max(s, o[j])) / risk
                        st.sl_hits += 1
                    elif l[j] <= t:
                        r = (e - t) / risk
                        st.tp_hits += 1
                if r is not None:
                    exit_j = j
                    break
            if r is None:
                if i + self.max_hold_bars <= n - 1:
                    exit_j = i + self.max_hold_bars
                    r = ((c[exit_j] - e) if side == "long" else (e - c[exit_j])) / risk
                    st.time_exits += 1
                else:
                    continue  # unresolved
            r -= cost_frac * e / risk
            rs.append(float(r))
            busy_until = exit_j
        st.n = len(rs)
        st.r_values = rs
        if rs:
            arr = np.array(rs)
            st.wins = int((arr > 0).sum())
            st.p = st.wins / st.n
            st.ci_low, st.ci_high = wilson(st.wins, st.n)
            st.mean_r = float(arr.mean())
            st.median_r = float(np.median(arr))
            gl = -arr[arr < 0].sum()
            st.profit_factor = float(arr[arr > 0].sum() / gl) if gl > 0 else None
        return st


def cross_up(a: np.ndarray, b: np.ndarray | float) -> np.ndarray:
    b_arr = b if isinstance(b, np.ndarray) else np.full(len(a), b)
    prev_a = np.concatenate([[np.nan], a[:-1]])
    prev_b = np.concatenate([[np.nan], b_arr[:-1]])
    return (prev_a < prev_b) & (a >= b_arr)


def cross_down(a: np.ndarray, b: np.ndarray | float) -> np.ndarray:
    b_arr = b if isinstance(b, np.ndarray) else np.full(len(a), b)
    prev_a = np.concatenate([[np.nan], a[:-1]])
    prev_b = np.concatenate([[np.nan], b_arr[:-1]])
    return (prev_a > prev_b) & (a <= b_arr)


def shift(a: np.ndarray, k: int = 1) -> np.ndarray:
    return np.concatenate([np.full(k, np.nan), a[:-k]]) if k > 0 else a
