"""Agent 2 - Technical Analyst: multi-timeframe analysis (1m ... 1d) of the top markets."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from app.agents.base import Agent
from app.agents.scanner import ScanItem
from app.config import get_settings
from app.data.base import TF_SECONDS, TIMEFRAMES, Candles
from app.data.hub import MarketData
from app.db import Analysis, session_scope
from app.indicators import Indicators, compute
from app.patterns import candlestick_patterns, market_structure, support_resistance, trend_label

LIMITS = {"1m": 240, "5m": 1000, "15m": 1000, "1h": 1000, "4h": 1000, "1d": 400}
ANNUAL = {"1m": 525600, "5m": 105120, "15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}


@dataclass
class MarketAnalysis:
    symbol: str
    asset_class: str
    scan: ScanItem
    ind: dict[str, Indicators] = field(default_factory=dict)
    raw: dict[str, Candles] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _f(x, nd=6):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


class TechnicalAnalyst(Agent):
    name = "technical"

    def __init__(self, md: MarketData, prefix: str = ""):
        super().__init__(prefix)
        self.md = md
        self.st = get_settings()

    def run(self, cycle_id: int, ranked: list[ScanItem], extra_symbols: list[tuple[str, str]] | None = None
            ) -> list[MarketAnalysis]:
        self.cycle_id = cycle_id
        with self.task("Multi-Timeframe-Analyse") as res:
            picks = [it for it in ranked if not (it.ticker.asset_class == "stock" and not it.ticker.market_open)]
            picks = picks[: self.st.analysis_top_n]
            with ThreadPoolExecutor(max_workers=4) as ex:
                out = list(ex.map(self._analyse, picks))
            out = [a for a in out if a.ind]
            self._persist(cycle_id, out)
            res.update({
                "analysed": len(out),
                "symbols": [a.symbol for a in out],
                "timeframes": TIMEFRAMES,
                "summary": {a.symbol: {"alignment": a.summary.get("alignment"),
                                        "trend_1h": a.summary.get("tf", {}).get("1h", {}).get("trend"),
                                        "rsi_1h": a.summary.get("tf", {}).get("1h", {}).get("rsi")} for a in out},
                "errors": {a.symbol: a.errors for a in out if a.errors},
            })
            if any(a.errors for a in out):
                res["_status"] = "warning"
            lines = []
            for a in out[:8]:
                t = a.summary.get("tf", {})
                lines.append(f"{a.symbol}: Ausrichtung {a.summary.get('alignment'):+.2f}, 1h {t.get('1h', {}).get('trend')}"
                             f" RSI {t.get('1h', {}).get('rsi')}, Struktur {t.get('1h', {}).get('structure')}")
            self.decision(f"{len(out)} Märkte über {len(TIMEFRAMES)} Zeitrahmen analysiert. " + "; ".join(lines),
                          {"symbols": [a.symbol for a in out]})
            return out

    def _analyse(self, it: ScanItem) -> MarketAnalysis:
        t = it.ticker
        ma = MarketAnalysis(symbol=t.symbol, asset_class=t.asset_class, scan=it)
        now = self.md.clock()
        tf_summary = {}
        for tf in TIMEFRAMES:
            try:
                c = self.md.candles(t.symbol, tf, LIMITS[tf], asset_class=t.asset_class)
            except Exception as e:  # noqa: BLE001
                ma.errors[tf] = str(e)[:200]
                continue
            closed = c.closed(now)
            if len(closed) < 30:
                ma.errors[tf] = f"zu wenige Kerzen ({len(closed)})"
                continue
            ind = compute(closed)
            ma.ind[tf] = ind
            ma.raw[tf] = c
            tf_summary[tf] = self._summarise(tf, ind, closed, now)
        ups = sum(1 for s in tf_summary.values() if s["trend"] == "up")
        downs = sum(1 for s in tf_summary.values() if s["trend"] == "down")
        ma.summary = {
            "symbol": t.symbol, "asset_class": t.asset_class, "price": t.price,
            "alignment": (ups - downs) / max(len(tf_summary), 1), "tf": tf_summary,
            "scan_flags": it.flags,
        }
        return ma

    def _summarise(self, tf: str, ind: Indicators, c: Candles, now: float) -> dict:
        i = -1
        s = {
            "bars": len(c), "last_bar_time": float(c.t[-1]), "stale": c.is_stale(now),
            "close": _f(ind.c[i], 8), "rsi": _f(ind.rsi[i], 1), "macd": _f(ind.macd[i]), "macd_signal": _f(ind.macd_signal[i]),
            "macd_hist": _f(ind.macd_hist[i]), "ema20": _f(ind.ema20[i], 8), "ema50": _f(ind.ema50[i], 8),
            "ema200": _f(ind.ema200[i], 8), "bb_upper": _f(ind.bb_upper[i], 8), "bb_lower": _f(ind.bb_lower[i], 8),
            "bb_pctb": _f(ind.bb_pctb[i], 2), "bb_width_pct": _f(ind.bb_width[i] * 100, 2),
            "atr": _f(ind.atr[i], 8), "atr_pct": _f(ind.atr_pct[i], 3), "adx": _f(ind.adx[i], 1),
            "volume_ratio": _f(ind.vol_ratio[i], 2),
            "volatility_annual_pct": _f(ind.realized_vol[i] * math.sqrt(ANNUAL[tf]) * 100, 1),
            "trend": trend_label(ind),
            "structure": market_structure(ind)["structure"],
            "patterns": [p["name"] for p in candlestick_patterns(ind)],
        }
        if tf in ("1h", "1d"):
            sr = support_resistance(ind)
            s["support"] = _f(sr["nearest_support"], 8)
            s["resistance"] = _f(sr["nearest_resistance"], 8)
            s["sr_zones"] = {"supports": sr["supports"][:3], "resistances": sr["resistances"][:3]}
        return s

    def _persist(self, cycle_id: int, out: list[MarketAnalysis]) -> None:
        with session_scope() as s:
            for a in out:
                s.add(Analysis(cycle_id=cycle_id, symbol=a.symbol, asset_class=a.asset_class,
                               summary=_jsonable(a.summary)))


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


__all__ = ["TechnicalAnalyst", "MarketAnalysis", "TF_SECONDS"]
