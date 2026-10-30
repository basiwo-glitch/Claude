"""Agent 3 - AI Strategy Analyst.

For every analysed market and every enabled strategy it checks the rule on the
last *closed* bar. Probabilities are never invented: the identical rule is replayed
on the available history of all analysed markets of the same asset class
(non-overlapping trades, conservative fills, costs deducted) giving

* hit rate p with a 95 % Wilson confidence interval and the sample size,
* expected value in R (multiples of the initial risk) net of fees & slippage.

Once a strategy has >= 10 closed paper trades, the forward-test expectancy is
also used and the more conservative value wins.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from app.agents.base import Agent
from app.agents.technical import MarketAnalysis, _jsonable
from app.config import get_settings
from app.db import Position, Signal, StrategyState, session_scope
from app.engine.costs import round_trip_cost_frac
from app.strategies import STRATEGIES
from app.strategies.base import HistoryStats, wilson


@dataclass
class LiveStats:
    n: int = 0
    mean_r: float | None = None
    win_rate: float | None = None


def pooled(stats: list[HistoryStats]) -> HistoryStats:
    rs = [r for s in stats for r in s.r_values]
    out = HistoryStats(n=len(rs), r_values=rs, bars_used=sum(s.bars_used for s in stats),
                       tp_hits=sum(s.tp_hits for s in stats), sl_hits=sum(s.sl_hits for s in stats),
                       time_exits=sum(s.time_exits for s in stats))
    if rs:
        a = np.array(rs)
        out.wins = int((a > 0).sum())
        out.p = out.wins / out.n
        out.ci_low, out.ci_high = wilson(out.wins, out.n)
        out.mean_r = float(a.mean())
        out.median_r = float(np.median(a))
        gl = -a[a < 0].sum()
        out.profit_factor = float(a[a > 0].sum() / gl) if gl > 0 else None
    return out


def live_stats(account: str) -> dict[str, LiveStats]:
    out: dict[str, LiveStats] = {}
    with session_scope() as s:
        q = s.query(Position.strategy, Position.r_multiple).filter(Position.account == account,
                                                                   Position.status == "closed")
        if account == "PAPER":
            q = q.filter(Position.is_test.is_(False))
        rows = q.all()
    by: dict[str, list[float]] = defaultdict(list)
    for strat, r in rows:
        if r is not None:
            by[strat].append(r)
    for k, v in by.items():
        a = np.array(v)
        out[k] = LiveStats(n=len(v), mean_r=float(a.mean()), win_rate=float((a > 0).mean()))
    return out


class StrategyAnalyst(Agent):
    name = "strategist"

    def __init__(self, md, prefix: str = ""):
        super().__init__(prefix)
        self.md = md
        self.st = get_settings()

    def enabled_strategies(self) -> dict[str, bool]:
        with session_scope() as s:
            rows = {r.name: r.enabled for r in s.query(StrategyState).all()}
        return {name: rows.get(name, True) for name in STRATEGIES}

    def run(self, cycle_id: int, analyses: list[MarketAnalysis], account: str, is_test: bool
            ) -> tuple[list[int], dict]:
        self.cycle_id = cycle_id
        with self.task("Signale bewerten") as res:
            enabled = self.enabled_strategies()
            live = live_stats(account)
            est_notional = self.st.starting_capital * self.st.max_position_pct / 100 / 2
            hist: dict[tuple[str, str, str], list[HistoryStats]] = defaultdict(list)
            per_symbol: dict[tuple[str, str, str], HistoryStats] = {}
            candidates = []
            near_miss: list[tuple[int, str, str, str, int]] = []
            skipped: list[str] = []
            for a in analyses:
                for sname, strat in STRATEGIES.items():
                    tf = strat.timeframe(a.asset_class)
                    ind = a.ind.get(tf)
                    if ind is None or len(ind) < strat.warmup + 20:
                        continue
                    conds = strat.conditions(ind)
                    lo, sh = strat.entries(ind, conds)
                    cost = round_trip_cost_frac(a.asset_class, a.scan.ticker.quote_volume, est_notional, self.st)
                    for side, arr in (("long", lo), ("short", sh)):
                        if side == "short" and not self.st.allow_shorts:
                            continue
                        hs = strat.replay(ind, side, cost, entries=arr)
                        hist[(sname, side, a.asset_class)].append(hs)
                        per_symbol[(sname, side, a.symbol)] = hs
                        if arr[-1]:
                            if not enabled.get(sname, True):
                                skipped.append(f"{a.symbol}/{sname}/{side}: Strategie deaktiviert")
                                continue
                            if a.summary["tf"].get(tf, {}).get("stale"):
                                skipped.append(f"{a.symbol}/{sname}/{side}: Daten veraltet ({tf})")
                                continue
                            candidates.append((a, sname, side, tf, ind, conds, cost))
                        else:
                            expl = strat.explain(ind, len(ind) - 1, side, conds)
                            met = sum(1 for e in expl if e["met"])
                            near_miss.append((met, len(expl), a.symbol, sname, side))
            pooled_stats = {k: pooled(v) for k, v in hist.items()}
            signal_ids: list[int] = []
            with session_scope() as s:
                for a, sname, side, tf, ind, conds, cost in candidates:
                    bar_t = float(ind.candles.t[-1])
                    seen = s.query(Signal.id, Signal.status).filter(
                        Signal.account == account, Signal.symbol == a.symbol, Signal.strategy == sname,
                        Signal.side == side, Signal.bar_time == bar_t).first()
                    if seen:
                        skipped.append(f"{a.symbol}/{sname}/{side}: Signal dieser Kerze bereits bewertet "
                                       f"(#{seen[0]}, {seen[1]})")
                        continue
                    sig = self._build_signal(cycle_id, account, is_test, a, sname, side, tf, ind, conds, cost,
                                             pooled_stats.get((sname, side, a.asset_class)) or HistoryStats(),
                                             per_symbol.get((sname, side, a.symbol)) or HistoryStats(),
                                             live.get(sname))
                    s.add(sig)
                    s.flush()
                    signal_ids.append(sig.id)
            # scenario overview for the dashboard / log
            scenarios = {a.symbol: self._scenarios(a, pooled_stats) for a in analyses[:12]}
            near_miss.sort(key=lambda x: -(x[0] / x[1]))
            res.update({
                "signals": len(signal_ids), "candidates_checked": len(analyses) * len(STRATEGIES) * 2,
                "skipped": skipped[:20],
                "closest_setups": [f"{m}/{n} Bedingungen: {sym} {st_} {sd}" for m, n, sym, st_, sd in near_miss[:5]],
                "model": {f"{k[0]}:{k[1]}:{k[2]}": {"n": v.n, "p": _r(v.p), "ev_r": _r(v.mean_r)}
                          for k, v in pooled_stats.items()},
                "scenarios": scenarios,
            })
            if signal_ids:
                self.decision(f"{len(signal_ids)} Handelssignal(e) erzeugt und an den Risk Manager übergeben.",
                              {"signal_ids": signal_ids})
            else:
                self.decision("Kein Signal: auf keinem analysierten Markt sind alle Regeln einer Strategie erfüllt. "
                              "Am nächsten dran: " + "; ".join(res["closest_setups"][:3]),
                              {"closest": res["closest_setups"], "skipped": skipped[:10]})
            return signal_ids, res

    # ------------------------------------------------------------------------------------
    def _build_signal(self, cycle_id, account, is_test, a: MarketAnalysis, sname, side, tf, ind, conds, cost,
                      pool: HistoryStats, sym: HistoryStats, live: LiveStats | None) -> Signal:
        strat = STRATEGIES[sname]
        sl_arr, tp_arr = strat.levels(ind, side)
        e, slv, tpv = float(ind.c[-1]), float(sl_arr[-1]), float(tp_arr[-1])
        risk = abs(e - slv)
        rr = abs(tpv - e) / risk if risk > 0 else 0.0
        cost_r = cost * e / risk if risk > 0 else math.inf
        ev = pool.mean_r
        ev_source = f"Replay derselben Regel: {pool.n} nicht-überlappende Trades auf {a.asset_class}-Märkten"
        if live and live.n >= 10 and live.mean_r is not None and ev is not None:
            if live.mean_r < ev:
                ev = live.mean_r
                ev_source = f"Forward-Test ({live.n} Paper-Trades) – konservativer als Replay"
        tsum = a.summary["tf"].get(tf, {})
        rationale = {
            "strategy": strat.label, "strategy_description": strat.description, "timeframe": tf,
            "conditions": strat.explain(ind, len(ind) - 1, side, conds),
            "bar_time": float(ind.candles.t[-1]),
            "indicators": {k: tsum.get(k) for k in ("rsi", "macd_hist", "ema20", "ema50", "ema200", "atr", "atr_pct",
                                                    "adx", "bb_pctb", "volume_ratio", "trend", "structure",
                                                    "patterns")},
            "mtf_alignment": a.summary.get("alignment"),
            "mtf_trends": {k: v.get("trend") for k, v in a.summary["tf"].items()},
            "levels": {"entry": e, "stop_loss": slv, "take_profit": tpv,
                       "stop_rule": f"{strat.sl_atr} × ATR", "target_rule": "Bollinger-Mitte" if sname == "mean_reversion"
                       else f"{strat.tp_atr} × ATR", "max_hold_bars": strat.max_hold_bars},
            "support_resistance": {"support": a.summary["tf"].get("1h", {}).get("support"),
                                   "resistance": a.summary["tf"].get("1h", {}).get("resistance")},
            "model": {"source": ev_source, "pooled": pool.as_dict(), "symbol": sym.as_dict(),
                      "live": live.__dict__ if live else None, "cost_r": round(cost_r, 4),
                      "cost_pct_round_trip": round(cost * 100, 4)},
            "scan_flags": a.scan.flags,
            "quote_volume": a.scan.ticker.quote_volume,
            "text": self._text(a, strat, side, tf, e, slv, tpv, rr, pool, ev, cost_r),
        }
        return Signal(
            cycle_id=cycle_id, account=account, symbol=a.symbol, asset_class=a.asset_class, strategy=sname,
            side=side, timeframe=tf, bar_time=float(ind.candles.t[-1]), entry=e, stop_loss=slv, take_profit=tpv, probability=pool.p,
            prob_ci_low=pool.ci_low, prob_ci_high=pool.ci_high, sample_size=pool.n, expected_value_r=ev,
            reward_risk=rr, cost_r=cost_r, rationale=_jsonable(rationale), status="proposed", is_test=is_test,
        )

    @staticmethod
    def _text(a, strat, side, tf, e, sl, tp, rr, pool: HistoryStats, ev, cost_r) -> str:
        dir_ = "Long" if side == "long" else "Short"
        p = f"{pool.p * 100:.0f} % (95 %-KI {pool.ci_low * 100:.0f}–{pool.ci_high * 100:.0f} %, n={pool.n})" \
            if pool.p is not None else "keine historischen Fälle"
        evs = f"{ev:+.2f} R" if ev is not None else "unbekannt"
        return (f"{dir_} {a.symbol} ({strat.label}, {tf}): alle Regeln erfüllt. Einstieg ~{e:.6g}, Stop {sl:.6g}, "
                f"Ziel {tp:.6g} (CRV {rr:.2f}). Gemessene Trefferquote der Regel: {p}; erwarteter Wert nach "
                f"Kosten: {evs} (Kosten ≈ {cost_r:.2f} R).")

    @staticmethod
    def _scenarios(a: MarketAnalysis, pooled_stats: dict) -> dict:
        h1 = a.summary["tf"].get("1h", {})
        d1 = a.summary["tf"].get("1d", {})
        sup, resi = h1.get("support"), h1.get("resistance")
        best_long = max(((k[0], v) for k, v in pooled_stats.items() if k[1] == "long" and k[2] == a.asset_class
                         and v.mean_r is not None and v.n >= 10), key=lambda kv: kv[1].mean_r, default=None)
        best_short = max(((k[0], v) for k, v in pooled_stats.items() if k[1] == "short" and k[2] == a.asset_class
                          and v.mean_r is not None and v.n >= 10), key=lambda kv: kv[1].mean_r, default=None)
        return {
            "long": {"trigger": f"Stundenschluss über Widerstand {resi:.6g}" if resi else "kein Widerstand erkannt",
                     "invalidation": f"unter Unterstützung {sup:.6g}" if sup else None,
                     "context": f"1h-Trend {h1.get('trend')}, 1d-Trend {d1.get('trend')}, RSI(1h) {h1.get('rsi')}",
                     "best_rule": f"{best_long[0]}: EV {best_long[1].mean_r:+.2f} R (n={best_long[1].n})" if best_long else None},
            "short": {"trigger": f"Stundenschluss unter Unterstützung {sup:.6g}" if sup else "keine Unterstützung erkannt",
                      "invalidation": f"über Widerstand {resi:.6g}" if resi else None,
                      "context": f"Struktur {h1.get('structure')}, ADX(1h) {h1.get('adx')}",
                      "best_rule": f"{best_short[0]}: EV {best_short[1].mean_r:+.2f} R (n={best_short[1].n})" if best_short else None},
        }


def _r(x, nd=3):
    return None if x is None else round(x, nd)
