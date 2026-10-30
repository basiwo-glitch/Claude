"""Controlled integration test with an ARTIFICIAL market data set.

The data set is constructed so that the breakout rule has a long, clean history
(squeeze -> breakout on volume -> continuation). At the end of the data a fresh
breakout bar closes, so the full pipeline MUST produce, approve and execute an
order; the clock is then advanced until the take-profit is reached.

Everything is booked on the account ``SELFTEST``; every signal/order/position is
flagged ``is_test=True`` and agent states use the prefix ``selftest:``. These
trades never count as trading performance.

Run:  python -m app.selftest            (stores the result in system_state 'selftest')
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time

import numpy as np

from app import clock
from app.data.base import Candles
from app.data.fx import FxService
from app.data.hub import MarketData
from app.data.synthetic import ScenarioProvider
from app.db import (AgentLog, AgentState, Cycle, Order, Position, Signal, init_db, now, session_scope,
                    set_state)

log = logging.getLogger("selftest")
ACCOUNT = "SELFTEST"
PREFIX = "selftest:"


def _segment(t0: float, n: int, p_from: float, p_to: float, vol: float, wiggle: float, rng, osc_period=97.0):
    m = np.arange(n)
    path = p_from + (p_to - p_from) * (m + 1) / n
    path = path * (1 + wiggle * np.sin(2 * math.pi * m / osc_period) + wiggle * 0.3 * rng.standard_normal(n))
    t = t0 + 60 * m
    return t, path, np.full(n, vol) * (0.8 + 0.4 * rng.random(n))


def build_breakout_series(symbol: str, base: float, start: float, cycles: int = 36, seed: int = 1,
                          final_continuation: bool = True) -> tuple[Candles, float]:
    """Returns (1m candles, time when the final breakout bar has closed)."""
    rng = np.random.default_rng(seed)
    ts, ps, vs = [], [], []
    t = start
    level = base
    final_close = None
    for k in range(cycles):
        # 30h tight consolidation (squeeze), low volume
        a = _segment(t, 30 * 60, level, level, 1_000.0, 0.0018, rng)
        t += 30 * 3600
        # 1h breakout bar: strong close above the range on 6x volume
        b = _segment(t, 60, level * 1.004, level * 1.02, 6_000.0, 0.0004, rng, osc_period=29)
        t += 3600
        parts = [a, b]
        if k == cycles - 1:
            final_close = t
            if not final_continuation:
                for x in parts:
                    ts.append(x[0]), ps.append(x[1]), vs.append(x[2])
                break
        # 10h continuation (> 3 ATR), then the new level
        c = _segment(t, 10 * 60, level * 1.02, level * 1.075, 2_500.0, 0.0006, rng, osc_period=41)
        t += 10 * 3600
        parts.append(c)
        level *= 1.07
        for x in parts:
            ts.append(x[0]), ps.append(x[1]), vs.append(x[2])
    tt = np.concatenate(ts)
    close = np.concatenate(ps)
    vol = np.concatenate(vs)
    op = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(op, close) * 1.0002
    lo = np.minimum(op, close) * 0.9998
    return Candles(symbol, "1m", tt.astype(float), op, hi, lo, close, vol, source="selftest"), float(final_close)


def purge_previous() -> None:
    with session_scope() as s:
        pids = [p.id for p in s.query(Position.id).filter(Position.account == ACCOUNT)]
        s.query(Order).filter(Order.account == ACCOUNT).delete(synchronize_session=False)
        if pids:
            s.query(Position).filter(Position.id.in_(pids)).delete(synchronize_session=False)
        s.query(Signal).filter(Signal.account == ACCOUNT).delete(synchronize_session=False)
        cids = [c.id for c in s.query(Cycle.id).filter(Cycle.account == ACCOUNT)]
        if cids:
            from app.db import Analysis, ScanResult

            s.query(ScanResult).filter(ScanResult.cycle_id.in_(cids)).delete(synchronize_session=False)
            s.query(Analysis).filter(Analysis.cycle_id.in_(cids)).delete(synchronize_session=False)
            s.query(Cycle).filter(Cycle.id.in_(cids)).delete(synchronize_session=False)
        s.query(AgentLog).filter(AgentLog.agent.like(f"{PREFIX}%")).delete(synchronize_session=False)
        s.query(AgentState).filter(AgentState.name.like(f"{PREFIX}%")).delete(synchronize_session=False)
        from app.db import Account, EquitySnapshot

        s.query(EquitySnapshot).filter(EquitySnapshot.account == ACCOUNT).delete(synchronize_session=False)
        s.query(Account).filter(Account.name == ACCOUNT).delete(synchronize_session=False)


def run_selftest(store: bool = True) -> dict:
    """Runs the controlled integration test. Uses a simulated clock (call in its own process)."""
    from app.engine.orchestrator import Orchestrator

    started = time.time()
    checks: list[dict] = []

    def check(name: str, ok: bool, detail) -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    start = 1_700_000_000.0 - (1_700_000_000.0 % 3600)
    series, final_close = {}, None
    for i, (sym, base) in enumerate((("TESTAAAUSDT", 100.0), ("TESTBBBUSDT", 2.5), ("TESTCCCUSDT", 40.0))):
        series[sym], fc = build_breakout_series(sym, base, start, seed=i + 1)
        final_close = fc
    ref = [final_close + 5.0]
    clock.set_clock(ref)
    try:
        purge_previous()
        md = MarketData(providers={"crypto": ScenarioProvider(series, ref)}, fx=FxService(fixed={"USD": 0.9}),
                        synthetic=True)
        orch = Orchestrator(md, account=ACCOUNT, is_test=True, agent_prefix=PREFIX)
        with session_scope() as s:
            paper_before = s.query(Position).filter(Position.account == "PAPER").count()

        r1 = orch.run_cycle()
        check("Zyklus läuft durch", r1.get("status") == "ok", r1.get("error") or "ok")
        check("Marktdaten geladen (künstlicher Datensatz)", r1.get("markets_scanned", 0) == 3,
              f"{r1.get('markets_scanned')} Märkte")
        check("Technische Analyse", r1.get("analysed", 0) >= 1, f"{r1.get('analysed')} Märkte analysiert")
        with session_scope() as s:
            sigs = s.query(Signal).filter(Signal.account == ACCOUNT, Signal.strategy == "breakout").all()
            sig_info = [{"symbol": x.symbol, "status": x.status, "n": x.sample_size,
                         "p": round(x.probability or 0, 3), "ev_r": round(x.expected_value_r or 0, 3),
                         "veto": x.veto_reason} for x in sigs]
        check("Breakout-Signal erzeugt", len(sigs) >= 1, sig_info)
        check("Risikoprüfung gibt frei", any(x["status"] == "executed" for x in sig_info), sig_info)
        with session_scope() as s:
            pos = s.query(Position).filter(Position.account == ACCOUNT).all()
            opened = [p for p in pos if p.strategy == "breakout"]
            ok_flags = all(p.is_test for p in pos) and all(
                o.is_test for o in s.query(Order).filter(Order.account == ACCOUNT))
        check("Paper-Order automatisch ausgeführt", len(opened) >= 1, f"{len(opened)} Position(en)")
        check("Testorder eindeutig gekennzeichnet (is_test, Konto SELFTEST)", ok_flags and len(pos) > 0, ACCOUNT)

        r2 = orch.run_cycle()  # same bar again -> must not create a second order
        with session_scope() as s:
            n_orders = s.query(Order).filter(Order.account == ACCOUNT, Order.kind == "entry").count()
        check("Doppelte Orders verhindert", n_orders == len(pos), f"{n_orders} Entry-Order(s) nach 2. Zyklus")

        # advance time minute by minute until all positions are closed (max 24h)
        for _ in range(24 * 60):
            ref[0] += 60
            orch.run_monitor()
            with session_scope() as s:
                if s.query(Position).filter(Position.account == ACCOUNT, Position.status == "open").count() == 0:
                    break
        with session_scope() as s:
            pos = s.query(Position).filter(Position.account == ACCOUNT).all()
            closed = [p for p in pos if p.status == "closed"]
            tp = [p for p in closed if p.exit_reason == "Take-Profit erreicht"]
            pnl_ok = True
            pnl_detail = []
            for p in closed:
                orders = {o.kind: o for o in s.query(Order).filter(Order.position_id == p.id)}
                e, x = orders["entry"], orders["exit"]
                sign = 1 if p.side == "long" else -1
                expected = sign * p.qty * (x.fill_price * p.fx_exit - e.fill_price * p.fx_entry) - e.fee_eur - x.fee_eur
                pnl_ok &= abs(expected - p.pnl_eur) < 1e-6
                pnl_detail.append({"symbol": p.symbol, "exit": p.exit_reason, "pnl_eur": round(p.pnl_eur, 4),
                                   "recomputed": round(expected, 4), "r": round(p.r_multiple or 0, 3)})
            from app.db import Account

            acc = s.query(Account).filter(Account.name == ACCOUNT).one()
            cash_expected = acc.starting_capital + sum(p.pnl_eur for p in closed)
            open_left = [p for p in pos if p.status == "open"]
            paper_after = s.query(Position).filter(Position.account == "PAPER").count()
        check("Positionen regelkonform geschlossen (Take-Profit)", len(tp) >= 1 and not open_left,
              [d["exit"] for d in pnl_detail])
        check("G&V korrekt berechnet (aus Orders nachgerechnet)", pnl_ok and len(closed) > 0, pnl_detail)
        check("Kontostand = Startkapital + realisierte G&V", abs(acc.cash - cash_expected) < 1e-6,
              f"{acc.cash:.4f} € vs. {cash_expected:.4f} €")
        check("Echte Performance unberührt", paper_after == paper_before,
              f"PAPER-Positionen vorher {paper_before}, nachher {paper_after}")
    except Exception as e:  # noqa: BLE001
        log.exception("selftest crashed")
        check("Selbsttest ohne Absturz", False, f"{type(e).__name__}: {e}")
    finally:
        clock.set_clock(None)
    result = {"ok": all(c["ok"] for c in checks), "checks": checks, "at": now(),
              "duration_sec": round(time.time() - started, 1)}
    if store:
        with session_scope() as s:
            set_state(s, "selftest", result)
    return result


def main() -> None:
    from app.logging_setup import setup_logging

    setup_logging("selftest")
    init_db()
    res = run_selftest()
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
