"""Agent 1 - Market Scanner.

Stage 1 (cheap, all markets): one 24h-ticker request for all Binance pairs and a
daily bar per stock -> filter illiquid / stable / leveraged markets, robust z-score
of the price change and liquidity score.
Stage 2 (top N): 1h candles -> volume spike (last closed hour vs 24h average),
unusual move (last hour return vs. 48h volatility), trend direction.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

from app.agents.base import Agent
from app.config import get_settings
from app.data.base import Ticker
from app.data.hub import MarketData
from app.db import ScanResult, session_scope
from app.indicators import ema


@dataclass
class ScanItem:
    ticker: Ticker
    score: float
    change_z: float = 0.0
    volume_ratio: float | None = None
    move_z: float | None = None
    trend: str | None = None
    flags: list[str] = field(default_factory=list)


def _robust_z(x: np.ndarray) -> np.ndarray:
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    return (x - med) / (mad if mad > 1e-9 else (np.std(x) or 1.0))


class MarketScanner(Agent):
    name = "scanner"

    def __init__(self, md: MarketData, prefix: str = ""):
        super().__init__(prefix)
        self.md = md
        self.st = get_settings()

    def run(self, cycle_id: int) -> tuple[list[ScanItem], dict]:
        self.cycle_id = cycle_id
        with self.task("Märkte scannen") as res:
            tickers, errors = self.md.tickers()
            universe = self._filter(tickers)
            stage1 = self._stage1(universe)
            deep = self._stage2(stage1[: self.st.deep_scan_top_n])
            rest = stage1[self.st.deep_scan_top_n:]
            ranked = sorted(deep, key=lambda s: -s.score) + rest
            self._persist(cycle_id, ranked[:150])
            by_class: dict[str, int] = {}
            for t in universe:
                by_class[t.asset_class] = by_class.get(t.asset_class, 0) + 1
            res.update({
                "markets_received": len(tickers), "markets_scanned": len(universe), "by_asset_class": by_class,
                "deep_scanned": len(deep), "provider_errors": errors,
                "top": [{"symbol": s.ticker.symbol, "score": round(s.score, 2), "flags": s.flags} for s in ranked[:5]],
            })
            if errors:
                res["_status"] = "warning" if universe else "error"
            if not universe:
                raise RuntimeError(f"keine Marktdaten erhalten: {errors}")
            spikes = [s.ticker.symbol for s in deep if "Volumenanstieg" in s.flags]
            unusual = [s.ticker.symbol for s in deep if "Ungewöhnliche Bewegung" in s.flags]
            self.decision(
                f"{len(universe)} Märkte gescannt ({', '.join(f'{k}: {v}' for k, v in by_class.items())}), "
                f"{len(deep)} vertieft. Volumenanstiege: {', '.join(spikes[:6]) or 'keine'}; "
                f"ungewöhnliche Bewegungen: {', '.join(unusual[:6]) or 'keine'}.",
                {"top": res["top"], "errors": errors},
            )
            return ranked, res

    # ------------------------------------------------------------------------------
    def _filter(self, tickers: list[Ticker]) -> list[Ticker]:
        out = []
        for t in tickers:
            if t.asset_class == "crypto" and not self.md.synthetic and t.quote_volume < self.st.crypto_min_quote_volume:
                continue
            if t.price <= 0 or not math.isfinite(t.change_pct):
                continue
            out.append(t)
        out.sort(key=lambda t: -t.quote_volume)
        return out[: self.st.max_markets_per_cycle]

    def _stage1(self, universe: list[Ticker]) -> list[ScanItem]:
        items: list[ScanItem] = []
        for ac in {t.asset_class for t in universe}:
            group = [t for t in universe if t.asset_class == ac]
            if not group:
                continue
            ch = np.array([t.change_pct for t in group])
            z = _robust_z(ch) if len(group) > 3 else np.zeros(len(group))
            lv = np.log10(np.array([max(t.quote_volume, 1.0) for t in group]))
            liq = (lv - lv.min()) / ((lv.max() - lv.min()) or 1.0)
            for t, zi, li in zip(group, z, liq):
                score = 0.6 * min(abs(zi), 6) + 1.2 * li
                if t.avg_volume_ratio:
                    score += 0.5 * min(t.avg_volume_ratio, 5)
                items.append(ScanItem(ticker=t, score=float(score), change_z=float(zi)))
        items.sort(key=lambda s: -s.score)
        return items

    def _deep_one(self, it: ScanItem) -> ScanItem:
        t = it.ticker
        if t.asset_class == "stock" and not t.market_open:
            it.flags.append("Markt geschlossen")
            return it
        try:
            c = self.md.candles(t.symbol, "1h", 60, asset_class=t.asset_class).closed(self.md.clock())
        except Exception as e:  # noqa: BLE001
            it.flags.append("Datenfehler")
            self.log.debug("deep scan %s failed: %s", t.symbol, e)
            return it
        if len(c) < 30:
            return it
        v = c.v
        base = float(np.mean(v[-25:-1])) if len(v) > 25 else float(np.mean(v[:-1]))
        it.volume_ratio = float(v[-1] / base) if base > 0 else None
        r = np.diff(np.log(c.c))
        sd = float(np.std(r[-49:-1])) if len(r) > 10 else 0.0
        it.move_z = float(r[-1] / sd) if sd > 0 else 0.0
        e20, e50 = ema(c.c, 20)[-1], ema(c.c, 50)[-1]
        it.trend = "flat" if np.isnan(e50) else ("up" if c.c[-1] > e20 > e50 else "down" if c.c[-1] < e20 < e50 else "flat")
        if it.volume_ratio and it.volume_ratio >= 2.0:
            it.flags.append("Volumenanstieg")
        if abs(it.move_z) >= 2.5:
            it.flags.append("Ungewöhnliche Bewegung")
        hi, lo = float(c.h[-21:-1].max()), float(c.l[-21:-1].min())
        if c.c[-1] >= hi:
            it.flags.append("20h-Hoch")
        elif c.c[-1] <= lo:
            it.flags.append("20h-Tief")
        it.score += 0.8 * min(it.volume_ratio or 0, 6) + 0.6 * min(abs(it.move_z), 6)
        return it

    def _stage2(self, items: list[ScanItem]) -> list[ScanItem]:
        with ThreadPoolExecutor(max_workers=6) as ex:
            return list(ex.map(self._deep_one, items))

    def _persist(self, cycle_id: int, ranked: list[ScanItem]) -> None:
        with session_scope() as s:
            for rank, it in enumerate(ranked, 1):
                t = it.ticker
                s.add(ScanResult(
                    cycle_id=cycle_id, symbol=t.symbol, asset_class=t.asset_class, price=t.price,
                    change_pct=t.change_pct, quote_volume=t.quote_volume, volume_ratio=it.volume_ratio,
                    move_zscore=it.move_z, trend=it.trend, score=it.score, rank=rank, flags=it.flags,
                    data_time=t.ts,
                ))
