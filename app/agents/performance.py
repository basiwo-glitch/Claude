"""Agent 6 - Performance Analyst.

Computes metrics from closed paper trades only (test trades excluded), compares
strategies, detects systematic errors with explicit evidence and sample sizes,
auto-disables strategies with proven negative results and writes reports.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

import numpy as np

from app.agents.base import Agent
from app.config import get_settings
from app.db import EquitySnapshot, Position, Report, StrategyState, now, session_scope
from app.engine.portfolio import TZ, account_snapshot, get_account
from app.strategies import STRATEGIES
from app.strategies.base import wilson


def max_drawdown(values: list[float]) -> tuple[float, float]:
    """(max drawdown in %, in absolute units) of an equity-like series."""
    if not values:
        return 0.0, 0.0
    a = np.array(values, dtype=float)
    peak = np.maximum.accumulate(a)
    dd_abs = peak - a
    with np.errstate(divide="ignore", invalid="ignore"):
        dd_pct = np.where(peak > 0, dd_abs / peak * 100, 0.0)
    return float(np.max(dd_pct)), float(np.max(dd_abs))


def trade_metrics(trades: list[Position], start_capital: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    pnl = np.array([t.pnl_eur or 0.0 for t in trades])
    rs = np.array([t.r_multiple for t in trades if t.r_multiple is not None])
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gp, gl = float(wins.sum()), float(-losses.sum())
    lo, hi = wilson(len(wins), n)
    eq = start_capital + np.cumsum(pnl)
    dd_pct, dd_abs = max_drawdown([start_capital] + list(eq))
    fees = float(sum((t.entry_fee_eur or 0) + (t.exit_fee_eur or 0) for t in trades))
    slip = float(sum(t.slippage_eur or 0 for t in trades))
    holds = [((t.exit_time or 0) - t.entry_time) / 60 for t in trades]
    exits: dict[str, int] = defaultdict(int)
    for t in trades:
        exits[t.exit_reason or "?"] += 1
    return {
        "trades": n, "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate": len(wins) / n, "win_rate_ci": [lo, hi],
        "profit_factor": (gp / gl) if gl > 0 else (None if gp == 0 else math.inf),
        "net_pnl_eur": float(pnl.sum()), "gross_profit_eur": gp, "gross_loss_eur": gl,
        "avg_win_eur": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_eur": float(losses.mean()) if len(losses) else 0.0,
        "expectancy_eur": float(pnl.mean()), "expectancy_r": float(rs.mean()) if len(rs) else None,
        "max_drawdown_pct": dd_pct, "max_drawdown_eur": dd_abs, "fees_eur": fees, "slippage_eur": slip,
        "avg_hold_min": float(np.mean(holds)), "exit_reasons": dict(exits),
        "best_eur": float(pnl.max()), "worst_eur": float(pnl.min()),
    }


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, float):
        if math.isinf(o):
            return "inf"
        if math.isnan(o):
            return None
        return round(o, 6)
    return o


class PerformanceAnalyst(Agent):
    name = "performance"

    def __init__(self, prefix: str = ""):
        super().__init__(prefix)
        self.st = get_settings()

    def run(self, account: str) -> dict:
        with self.task("Performance auswerten") as res:
            with session_scope() as s:
                acc = get_account(s, account)
                q = s.query(Position).filter(Position.account == account, Position.status == "closed")
                if account == "PAPER":  # the real paper account never counts test trades
                    q = q.filter(Position.is_test.is_(False))
                trades = q.order_by(Position.exit_time).all()
                snap = account_snapshot(s, acc)
                eq_rows = (s.query(EquitySnapshot.equity).filter(EquitySnapshot.account == account)
                           .order_by(EquitySnapshot.ts).all())
                calib = [(t_.rationale or {}).get("model", {}).get("pooled", {}).get("p") for t_ in trades]
                calib = [(p_, t_.pnl_eur) for p_, t_ in zip(calib, trades)]
                total = trade_metrics(trades, acc.starting_capital)
                eq_dd_pct, eq_dd_abs = max_drawdown([acc.starting_capital] + [e for (e,) in eq_rows] + [snap["equity"]])
                total["max_drawdown_pct_equity"] = eq_dd_pct
                by_strat = {}
                for name in STRATEGIES:
                    tr = [t for t in trades if t.strategy == name]
                    by_strat[name] = trade_metrics(tr, acc.starting_capital)
                    for side in ("long", "short"):
                        trs = [t for t in tr if t.side == side]
                        by_strat[name][f"{side}_trades"] = len(trs)
                        by_strat[name][f"{side}_expectancy_r"] = (float(np.mean([t.r_multiple for t in trs if t.r_multiple is not None]))
                                                                  if trs else None)
                findings = self._findings(trades, total, by_strat, calib)
                # strategy switches are global -> only the real paper account may change them
                changes = self._auto_disable(s, by_strat) if account == "PAPER" else []
                for name, m in (by_strat.items() if account == "PAPER" else []):
                    row = s.get(StrategyState, name)
                    if row is None:
                        row = StrategyState(name=name, enabled=True)
                        s.add(row)
                    row.stats = _clean(m)
                    row.updated_at = now()
                ranking = sorted(((k, v) for k, v in by_strat.items() if v.get("trades")),
                                 key=lambda kv: -(kv[1].get("expectancy_r") or -99))
                body = _clean({
                    "account": account, "generated_at": now(),
                    "equity": snap["equity"], "return_pct": (snap["equity"] / acc.starting_capital - 1) * 100,
                    "total": total, "by_strategy": by_strat, "findings": findings, "strategy_changes": changes,
                    "ranking": [k for k, _ in ranking],
                })
                last = s.query(Report).filter(Report.account == account).order_by(Report.created_at.desc()).first()
                if last is None or now() - last.created_at > 3600 or changes:
                    s.add(Report(account=account, created_at=now(), body=body))
            res.update({"trades": total.get("trades", 0), "win_rate": _clean(total.get("win_rate")),
                        "profit_factor": _clean(total.get("profit_factor")),
                        "max_drawdown_pct": _clean(total.get("max_drawdown_pct_equity")),
                        "findings": [f["title"] for f in findings][:6], "strategy_changes": changes})
            if total.get("trades", 0) == 0:
                self.decision("Noch keine abgeschlossenen Trades – keine belastbare Auswertung möglich.")
            else:
                self.decision(
                    f"{total['trades']} Trades: Trefferquote {total['win_rate'] * 100:.1f} %, Profit Factor "
                    f"{_fmt(total['profit_factor'])}, Netto {total['net_pnl_eur']:+.2f} €, max. DD {eq_dd_pct:.2f} %. "
                    + (" Erkenntnisse: " + "; ".join(f["title"] for f in findings[:3]) if findings else ""),
                    {"findings": findings[:6]})
            return res

    # -------------------------------------------------------------------------------
    def _auto_disable(self, s, by_strat: dict) -> list[str]:
        out = []
        for name, m in by_strat.items():
            n = m.get("trades", 0)
            pf = m.get("profit_factor")
            exp = m.get("expectancy_r")
            if n < self.st.auto_disable_min_trades:
                continue
            bad = (pf is not None and pf != math.inf and pf < self.st.auto_disable_profit_factor) and (exp is not None and exp < 0)
            row = s.get(StrategyState, name)
            if row is None:
                row = StrategyState(name=name, enabled=True)
                s.add(row)
            if bad and row.enabled:
                row.enabled = False
                row.disabled_by = "performance"
                row.disabled_reason = (f"Automatisch deaktiviert: {n} Trades, Profit Factor {pf:.2f} < "
                                       f"{self.st.auto_disable_profit_factor}, Erwartung {exp:+.2f} R")
                out.append(f"{name} deaktiviert ({row.disabled_reason})")
                self.decision(f"Strategie {name} deaktiviert: {row.disabled_reason}", level="WARNING")
        return out

    def _findings(self, trades: list[Position], total: dict, by_strat: dict, calib) -> list[dict]:
        f: list[dict] = []
        n = len(trades)
        if n == 0:
            return f
        weak = n < 30

        def add(title, evidence, suggestion, severity="info"):
            f.append({"title": title, "evidence": evidence, "suggestion": suggestion, "severity": severity,
                      "sample_size": n, "statistically_weak": weak})

        fees = total.get("fees_eur", 0) + total.get("slippage_eur", 0)
        gross_all = sum(abs(t.gross_pnl_eur or 0) for t in trades)
        if gross_all > 0 and fees / gross_all > 0.3:
            add("Kosten belasten das Ergebnis stark",
                f"Gebühren+Slippage {fees:.2f} € = {fees / gross_all * 100:.0f} % der absoluten Brutto-Bewegung",
                "Höhere Zeitrahmen / größere Ziele bevorzugen; Scalping-Anteil prüfen", "warning")
        sl = [t for t in trades if t.exit_reason == "Stop-Loss erreicht"]
        quick = [t for t in sl if ((t.exit_time or 0) - t.entry_time) < 2 * _tf_sec(t.timeframe)]
        if len(sl) >= 5 and len(quick) / len(sl) > 0.4:
            add("Viele schnelle Stop-Outs",
                f"{len(quick)} von {len(sl)} Stops innerhalb von 2 Kerzen ausgelöst",
                "Stop-Abstand (ATR-Faktor) testweise erhöhen und per Replay vergleichen", "warning")
        te = [t for t in trades if t.exit_reason == "Maximale Haltedauer erreicht"]
        if n >= 10 and len(te) / n > 0.4:
            avg = np.mean([t.r_multiple or 0 for t in te])
            add("Ziele werden selten erreicht",
                f"{len(te)} von {n} Trades endeten per Zeitstopp (Ø {avg:+.2f} R)",
                "Ziel (ATR-Faktor) verkleinern oder Haltedauer verlängern – per Replay prüfen")
        losses = [t for t in trades if (t.pnl_eur or 0) < 0]
        if len(losses) >= 5:
            by_sym: dict[str, float] = defaultdict(float)
            for t in losses:
                by_sym[t.symbol] += t.pnl_eur or 0
            sym, val = min(by_sym.items(), key=lambda kv: kv[1])
            tot = sum(by_sym.values())
            if tot < 0 and val / tot > 0.4:
                add(f"Verluste konzentriert auf {sym}", f"{sym}: {val:.2f} € von {tot:.2f} € Gesamtverlust",
                    f"{sym} temporär ausschließen oder Mindestliquidität erhöhen")
        for side in ("long", "short"):
            other = "short" if side == "long" else "long"
            rs = [t.r_multiple for t in trades if t.side == side and t.r_multiple is not None]
            ro = [t.r_multiple for t in trades if t.side == other and t.r_multiple is not None]
            if len(rs) >= 10 and len(ro) >= 10 and np.mean(rs) < 0 < np.mean(ro):
                add(f"{side.capitalize()}-Trades negativ",
                    f"Ø {np.mean(rs):+.2f} R ({len(rs)} Trades) vs. {other} Ø {np.mean(ro):+.2f} R ({len(ro)})",
                    f"{side.capitalize()}-Seite nur mit Trendfilter handeln", "warning")
        hours: dict[int, list[float]] = defaultdict(list)
        for t in trades:
            hours[datetime.fromtimestamp(t.entry_time, TZ).hour // 4 * 4].append(t.pnl_eur or 0)
        worst = min(((h, v) for h, v in hours.items() if len(v) >= 5), key=lambda kv: sum(kv[1]), default=None)
        if worst and sum(worst[1]) < 0 and len(hours) > 1:
            add(f"Schwaches Zeitfenster {worst[0]:02d}–{worst[0] + 4:02d} Uhr",
                f"{len(worst[1])} Trades, {sum(worst[1]):+.2f} €",
                "Zeitfilter für dieses Fenster im Forward-Test vergleichen")
        pairs = [(p, pnl) for p, pnl in calib if p is not None and pnl is not None]
        if len(pairs) >= 10:
            pred = float(np.mean([p for p, _ in pairs]))
            real = float(np.mean([1.0 if pnl > 0 else 0.0 for _, pnl in pairs]))
            if abs(pred - real) > 0.1:
                add("Modell-Kalibrierung weicht ab",
                    f"Vorhergesagte Trefferquote Ø {pred * 100:.0f} %, realisiert {real * 100:.0f} % ({len(pairs)} Trades)",
                    "Replay-Statistik überschätzt die Live-Ergebnisse – Mindest-EV anheben" if pred > real
                    else "Modell ist konservativ", "warning" if pred > real else "info")
        for name, m in by_strat.items():
            if m.get("trades", 0) >= 10 and (m.get("expectancy_r") or 0) < 0:
                add(f"Strategie {name} mit negativer Erwartung",
                    f"{m['trades']} Trades, Ø {m['expectancy_r']:+.2f} R, PF {_fmt(m.get('profit_factor'))}",
                    "Wird bei ≥ %d Trades und PF < %.1f automatisch deaktiviert" % (
                        self.st.auto_disable_min_trades, self.st.auto_disable_profit_factor), "warning")
        return f


def _tf_sec(tf: str) -> int:
    from app.data.base import TF_SECONDS

    return TF_SECONDS.get(tf, 3600)


def _fmt(x) -> str:
    if x is None:
        return "—"
    if x == math.inf or x == "inf":
        return "∞"
    return f"{x:.2f}"
