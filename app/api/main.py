"""REST API + dashboard hosting (FastAPI).

Read endpoints feed the dashboard; control endpoints (kill switch, strategies,
self-test) require the dashboard password (HTTP Basic auth over HTTPS).
"""
from __future__ import annotations

import logging
import math
import os
import secrets
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from sqlalchemy import func

from app.agents.base import AGENTS
from app.agents.performance import trade_metrics
from app.agents.risk import halt_key, kill_switch_state, set_kill_switch
from app.api.health import system_health
from app.brokers.live import live_gate_status
from app.config import get_settings
from app.data.base import TF_SECONDS
from app.data.hub import build_market_data
from app.db import (AgentLog, AgentState, Analysis, Cycle, EquitySnapshot, Order, Position, Report, ScanResult,
                    Signal, StrategyState, get_state, init_db, now, session_scope, set_state)
from app.engine.portfolio import account_snapshot, get_account
from app.indicators import compute
from app.logging_setup import setup_logging
from app.strategies import STRATEGIES

log = logging.getLogger("api")
STATIC = Path(__file__).resolve().parent.parent / "static"
security = HTTPBasic(auto_error=False)
_md = None
_md_lock = threading.Lock()


def md():
    global _md
    with _md_lock:
        if _md is None:
            _md = build_market_data()
        return _md


def require_auth(creds: HTTPBasicCredentials | None = Depends(security)) -> str:
    st = get_settings()
    if not st.dashboard_password:
        if st.env == "development":
            return "dev"
        raise HTTPException(503, "DASHBOARD_PASSWORD ist nicht gesetzt – Zugriff gesperrt.")
    if creds is None or not (secrets.compare_digest(creds.username, st.dashboard_user)
                             and secrets.compare_digest(creds.password, st.dashboard_password)):
        raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": 'Basic realm="AI Trader"'})
    return creds.username


@asynccontextmanager
async def lifespan(_app):
    setup_logging("api")
    init_db()
    if get_settings().run_worker_in_api:
        from app.worker import start_in_background

        start_in_background()
        log.info("worker started inside API process (RUN_WORKER_IN_API=true)")
    yield


app = FastAPI(title="AI Paper Trader", version="1.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)


def clean(o):
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        f = float(o)
        if math.isnan(f):
            return None
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        return f
    if isinstance(o, np.integer):
        return int(o)
    return o


def _acct() -> str:
    return get_settings().account_name


# ---------------------------------------------------------------------------- public
@app.get("/healthz")
def healthz():
    """Liveness for the container platform (no auth, no details)."""
    try:
        from app.db import db_ping

        db_ping()
        return {"ok": True}
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=503)


@app.get("/")
def index(_=Depends(require_auth)):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/static/{name}")
def static_file(name: str, _=Depends(require_auth)):
    path = (STATIC / name).resolve()
    if path.parent != STATIC.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------- read
@app.get("/api/health")
def health(_=Depends(require_auth)):
    return clean(system_health())


@app.get("/api/overview")
def overview(_=Depends(require_auth)):
    st = get_settings()
    acct = _acct()
    with session_scope() as s:
        acc = get_account(s, acct)
        snap = account_snapshot(s, acc)
        last = s.query(Cycle).filter(Cycle.account == acct).order_by(Cycle.id.desc()).first()
        last_ok = (s.query(Cycle).filter(Cycle.account == acct, Cycle.status == "ok")
                   .order_by(Cycle.id.desc()).first())
        n_cycles = s.query(func.count(Cycle.id)).filter(Cycle.account == acct).scalar()
        realized = (s.query(func.coalesce(func.sum(Position.pnl_eur), 0.0))
                    .filter(Position.account == acct, Position.status == "closed").scalar())
        closed_n = s.query(func.count(Position.id)).filter(Position.account == acct, Position.status == "closed").scalar()
        ks = kill_switch_state(s)
        halt = get_state(s, halt_key(acct), {"active": False})
        eq = snap["equity"]
        out = {
            "account": acct, "mode": st.data_mode, "synthetic": st.is_synthetic, "currency": "EUR",
            "starting_capital": acc.starting_capital, "equity": eq, "cash": acc.cash,
            "unrealized_pnl": snap["unrealized"], "realized_pnl": realized,
            "daily_pnl": eq - acc.day_start_equity, "daily_pnl_pct": (eq / acc.day_start_equity - 1) * 100,
            "total_return_pct": (eq / acc.starting_capital - 1) * 100,
            "drawdown_pct": (1 - eq / acc.peak_equity) * 100 if acc.peak_equity else 0,
            "gross_exposure": snap["gross_exposure"], "open_positions": snap["open_positions"],
            "closed_trades": closed_n, "cycles": n_cycles,
            "markets_analysed_last_cycle": (last_ok.markets_scanned if last_ok else 0),
            "deep_analysed_last_cycle": (last_ok.analysed if last_ok else 0),
            "last_cycle": _cycle_dict(last) if last else None,
            "kill_switch": ks, "halt": halt, "live_trading": False,
        }
    out["health"] = system_health()["overall"]
    return clean(out)


def _cycle_dict(c: Cycle) -> dict:
    return {"id": c.id, "started_at": c.started_at, "finished_at": c.finished_at, "status": c.status,
            "markets_scanned": c.markets_scanned, "candidates": c.candidates, "analysed": c.analysed,
            "signals": c.signals, "approved": c.approved, "executed": c.executed,
            "no_trade_reason": c.no_trade_reason, "error": c.error, "steps": c.steps}


@app.get("/api/cycles")
def cycles(limit: int = Query(30, le=200), _=Depends(require_auth)):
    with session_scope() as s:
        rows = s.query(Cycle).filter(Cycle.account == _acct()).order_by(Cycle.id.desc()).limit(limit).all()
        return clean([_cycle_dict(c) for c in rows])


@app.get("/api/scanner")
def scanner(limit: int = Query(100, le=150), _=Depends(require_auth)):
    with session_scope() as s:
        cyc = (s.query(Cycle).filter(Cycle.account == _acct(), Cycle.markets_scanned > 0)
               .order_by(Cycle.id.desc()).first())
        if not cyc:
            return {"cycle": None, "items": []}
        rows = s.query(ScanResult).filter(ScanResult.cycle_id == cyc.id).order_by(ScanResult.rank).limit(limit).all()
        analyses = {a.symbol: a.summary for a in s.query(Analysis).filter(Analysis.cycle_id == cyc.id)}
        sigs = {}
        for g in s.query(Signal).filter(Signal.cycle_id == cyc.id):
            sigs.setdefault(g.symbol, []).append(f"{g.side.upper()} {g.strategy} ({g.status})")
        items = []
        for r in rows:
            a = analyses.get(r.symbol)
            items.append({
                "rank": r.rank, "symbol": r.symbol, "asset_class": r.asset_class, "price": r.price,
                "change_pct": r.change_pct, "quote_volume": r.quote_volume, "volume_ratio": r.volume_ratio,
                "move_z": r.move_zscore, "trend": r.trend, "score": r.score, "flags": r.flags,
                "analysed": a is not None, "alignment": a.get("alignment") if a else None,
                "rsi_1h": (a or {}).get("tf", {}).get("1h", {}).get("rsi"),
                "signals": sigs.get(r.symbol, []),
            })
        return clean({"cycle": _cycle_dict(cyc), "items": items})


@app.get("/api/analysis/{symbol}")
def analysis(symbol: str, _=Depends(require_auth)):
    with session_scope() as s:
        a = (s.query(Analysis).join(Cycle, Cycle.id == Analysis.cycle_id).filter(
            Analysis.symbol == symbol, Cycle.account == _acct()).order_by(Analysis.id.desc()).first())
        return clean(a.summary if a else {})


@app.get("/api/agents")
def agents(_=Depends(require_auth)):
    out = []
    with session_scope() as s:
        for name, meta in AGENTS.items():
            a = s.get(AgentState, name)
            logs = (s.query(AgentLog).filter(AgentLog.agent == name).order_by(AgentLog.id.desc()).limit(8).all())
            out.append({
                "name": name, "no": meta["no"], "title": meta["title"],
                "status": a.status if a else "never", "current_task": a.current_task if a else None,
                "last_activity": a.last_activity if a else None, "last_success": a.last_success if a else None,
                "last_error": a.last_error if a else None, "last_error_at": a.last_error_at if a else None,
                "runs": a.runs if a else 0, "errors": a.errors if a else 0,
                "last_result": a.last_result if a else {},
                "decisions": [{"ts": x.ts, "level": x.level, "message": x.message, "cycle_id": x.cycle_id} for x in logs],
            })
    return clean(out)


@app.get("/api/logs")
def logs(agent: str | None = None, level: str | None = None, limit: int = Query(100, le=500),
         _=Depends(require_auth)):
    with session_scope() as s:
        q = s.query(AgentLog).filter(~AgentLog.agent.like("selftest:%"))
        if agent:
            q = q.filter(AgentLog.agent == agent)
        if level:
            q = q.filter(AgentLog.level == level.upper())
        rows = q.order_by(AgentLog.id.desc()).limit(limit).all()
        return clean([{"ts": r.ts, "agent": r.agent, "level": r.level, "message": r.message, "data": r.data,
                       "cycle_id": r.cycle_id} for r in rows])


def _signal_dict(g: Signal) -> dict:
    return {"id": g.id, "cycle_id": g.cycle_id, "created_at": g.created_at, "symbol": g.symbol,
            "asset_class": g.asset_class, "strategy": g.strategy, "side": g.side, "timeframe": g.timeframe,
            "entry": g.entry, "stop_loss": g.stop_loss, "take_profit": g.take_profit, "probability": g.probability,
            "prob_ci": [g.prob_ci_low, g.prob_ci_high], "sample_size": g.sample_size,
            "expected_value_r": g.expected_value_r, "reward_risk": g.reward_risk, "cost_r": g.cost_r,
            "status": g.status, "veto_reason": g.veto_reason, "rationale": g.rationale, "risk_notes": g.risk_notes,
            "is_test": g.is_test}


@app.get("/api/signals")
def signals(limit: int = Query(50, le=300), status: str | None = None, _=Depends(require_auth)):
    with session_scope() as s:
        q = s.query(Signal).filter(Signal.account == _acct())
        if status:
            q = q.filter(Signal.status == status)
        return clean([_signal_dict(g) for g in q.order_by(Signal.id.desc()).limit(limit)])


def _pos_dict(p: Position) -> dict:
    from app.engine.portfolio import unrealized_pnl_eur

    d = {c: getattr(p, c) for c in (
        "id", "symbol", "asset_class", "strategy", "side", "timeframe", "qty", "quote_ccy", "entry_price",
        "entry_ref_price", "entry_time", "stop_loss", "take_profit", "initial_risk_eur", "entry_fee_eur",
        "max_hold_sec", "status", "last_price", "last_price_time", "exit_price", "exit_time", "exit_reason",
        "exit_fee_eur", "gross_pnl_eur", "pnl_eur", "pnl_pct", "r_multiple", "slippage_eur", "client_order_id",
        "is_test", "signal_id")}
    d["rationale_text"] = (p.rationale or {}).get("text")
    d["exit_rationale"] = p.exit_rationale
    d["notional_eur"] = p.qty * p.entry_price * p.fx_entry
    if p.status == "open":
        d["unrealized_eur"] = unrealized_pnl_eur(p)
    return d


@app.get("/api/positions")
def positions(status: str = "open", limit: int = Query(200, le=1000), _=Depends(require_auth)):
    with session_scope() as s:
        q = s.query(Position).filter(Position.account == _acct(), Position.status == status)
        order = Position.entry_time.desc() if status == "open" else Position.exit_time.desc()
        return clean([_pos_dict(p) for p in q.order_by(order).limit(limit)])


@app.get("/api/positions/{pid}")
def position(pid: int, _=Depends(require_auth)):
    with session_scope() as s:
        p = s.get(Position, pid)
        if not p:
            raise HTTPException(404)
        d = _pos_dict(p)
        d["rationale"] = p.rationale
        d["orders"] = [{"id": o.id, "kind": o.kind, "side": o.side, "qty": o.qty, "ref_price": o.ref_price,
                        "fill_price": o.fill_price, "fee_eur": o.fee_eur, "slippage_bps": o.slippage_bps,
                        "created_at": o.created_at, "client_order_id": o.client_order_id, "reason": o.reason}
                       for o in s.query(Order).filter(Order.position_id == pid).order_by(Order.id)]
        return clean(d)


@app.get("/api/orders")
def orders(limit: int = Query(100, le=1000), _=Depends(require_auth)):
    with session_scope() as s:
        rows = s.query(Order).filter(Order.account == _acct()).order_by(Order.id.desc()).limit(limit).all()
        return clean([{"id": o.id, "created_at": o.created_at, "symbol": o.symbol, "side": o.side, "kind": o.kind,
                       "qty": o.qty, "ref_price": o.ref_price, "fill_price": o.fill_price, "fee_eur": o.fee_eur,
                       "slippage_bps": o.slippage_bps, "client_order_id": o.client_order_id, "reason": o.reason,
                       "position_id": o.position_id, "is_test": o.is_test} for o in rows])


@app.get("/api/performance")
def performance(_=Depends(require_auth)):
    acct = _acct()
    with session_scope() as s:
        acc = get_account(s, acct)
        trades = (s.query(Position).filter(Position.account == acct, Position.status == "closed",
                                           Position.is_test.is_(False) if acct == "PAPER" else True)
                  .order_by(Position.exit_time).all())
        total = trade_metrics(trades, acc.starting_capital)
        by = {name: trade_metrics([t for t in trades if t.strategy == name], acc.starting_capital) for name in STRATEGIES}
        states = {r.name: r for r in s.query(StrategyState).all()}
        strategies = []
        for name, strat in STRATEGIES.items():
            r = states.get(name)
            strategies.append({"name": name, "label": strat.label, "description": strat.description,
                               "timeframes": strat.timeframes, "enabled": r.enabled if r else True,
                               "disabled_reason": r.disabled_reason if r else None,
                               "disabled_by": r.disabled_by if r else None, "metrics": by[name]})
        snaps = (s.query(EquitySnapshot.ts, EquitySnapshot.equity).filter(EquitySnapshot.account == acct)
                 .order_by(EquitySnapshot.ts).all())
        step = max(1, len(snaps) // 1500)
        curve = [{"t": t, "equity": e} for t, e in snaps[::step]]
        if snaps and (not curve or curve[-1]["t"] != snaps[-1][0]):
            curve.append({"t": snaps[-1][0], "equity": snaps[-1][1]})
        eq = [e for _, e in snaps]
        peak, ddc = -1e18, []
        for t, e in snaps[::step]:
            peak = max(peak, e)
            ddc.append({"t": t, "dd": (e / peak - 1) * 100 if peak > 0 else 0})
        from app.agents.performance import max_drawdown

        total["max_drawdown_pct_equity"] = max_drawdown([acc.starting_capital] + eq)[0]
        rep = s.query(Report).filter(Report.account == acct).order_by(Report.created_at.desc()).first()
        return clean({"total": total, "strategies": strategies, "equity_curve": curve, "drawdown_curve": ddc,
                      "starting_capital": acc.starting_capital,
                      "report": {"created_at": rep.created_at, "findings": rep.body.get("findings", []),
                                 "ranking": rep.body.get("ranking", []),
                                 "strategy_changes": rep.body.get("strategy_changes", [])} if rep else None})


@app.get("/api/candles")
def candles(symbol: str, tf: str = "1h", limit: int = Query(300, le=1000), asset_class: str | None = None,
            _=Depends(require_auth)):
    if tf not in TF_SECONDS:
        raise HTTPException(400, "unbekannter Zeitrahmen")
    with session_scope() as s:
        if asset_class is None:
            r = s.query(ScanResult.asset_class).filter(ScanResult.symbol == symbol).order_by(ScanResult.id.desc()).first()
            asset_class = r[0] if r else None
        pos = s.query(Position).filter(Position.account == _acct(), Position.symbol == symbol).order_by(
            Position.entry_time.desc()).limit(30).all()
        markers = []
        lines = []
        for p in pos:
            markers.append({"time": p.entry_time, "type": "entry", "side": p.side, "price": p.entry_price,
                            "text": f"{'BUY' if p.side == 'long' else 'SELL'} {p.strategy}"})
            if p.exit_time:
                markers.append({"time": p.exit_time, "type": "exit", "side": p.side, "price": p.exit_price,
                                "text": f"EXIT {p.exit_reason or ''} {p.pnl_eur:+.2f}€" if p.pnl_eur is not None else "EXIT"})
            if p.status == "open":
                lines += [{"price": p.entry_price, "kind": "entry", "label": f"Entry #{p.id}"},
                          {"price": p.stop_loss, "kind": "sl", "label": f"SL #{p.id}"},
                          {"price": p.take_profit, "kind": "tp", "label": f"TP #{p.id}"}]
    try:
        c = md().candles(symbol, tf, max(limit + 200, 400), asset_class=asset_class)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Kursdaten nicht verfügbar: {e}") from e
    ind = compute(c)
    n = len(c)
    a = max(0, n - limit)

    def series(arr):
        return [None if not np.isfinite(x) else float(x) for x in arr[a:]]

    return clean({
        "symbol": symbol, "tf": tf, "source": c.source, "stale": c.is_stale(),
        "t": [int(x) for x in c.t[a:]], "o": series(c.o), "h": series(c.h), "l": series(c.l), "c": series(c.c),
        "v": series(c.v),
        "ind": {"ema20": series(ind.ema20), "ema50": series(ind.ema50), "ema200": series(ind.ema200),
                "bb_upper": series(ind.bb_upper), "bb_lower": series(ind.bb_lower), "bb_mid": series(ind.bb_mid),
                "rsi": series(ind.rsi), "macd": series(ind.macd), "macd_signal": series(ind.macd_signal),
                "macd_hist": series(ind.macd_hist)},
        "markers": markers, "lines": lines,
    })


@app.get("/api/strategies")
def strategies(_=Depends(require_auth)):
    return performance()["strategies"]


@app.get("/api/live-gate")
def live_gate(_=Depends(require_auth)):
    with session_scope() as s:
        return clean(live_gate_status(s))


@app.get("/api/selftest")
def selftest_result(_=Depends(require_auth)):
    with session_scope() as s:
        return clean({"result": get_state(s, "selftest"), "running": get_state(s, "selftest_running")})


# ---------------------------------------------------------------------------- control
class KillSwitchBody(BaseModel):
    active: bool
    reason: str = ""
    flatten: bool = False


@app.post("/api/kill-switch")
def kill_switch(body: KillSwitchBody, user=Depends(require_auth)):
    with session_scope() as s:
        v = set_kill_switch(s, body.active, body.reason or ("manuell aktiviert" if body.active else "manuell aufgehoben"),
                            user, body.flatten)
        s.add(AgentLog(agent="risk", level="WARNING", message=f"Kill Switch {'AKTIVIERT' if body.active else 'deaktiviert'} "
                       f"durch {user}: {v['reason']}" + (" – alle Positionen werden glattgestellt" if v["flatten"] else ""),
                       data=v, ts=now()))
        return v


@app.post("/api/kill-switch/test")
def kill_switch_test(user=Depends(require_auth)):
    """Verifies the emergency stop: activate -> a risk review must veto -> restore."""
    from types import SimpleNamespace

    from app.agents.risk import RiskManager

    with session_scope() as s:
        before = kill_switch_state(s)
        set_kill_switch(s, True, "Test der Notabschaltung", user)
    try:
        rm = RiskManager(md())
        fake = SimpleNamespace(strategy=next(iter(STRATEGIES)), symbol="__KILLSWITCH_TEST__", side="long",
                               asset_class="crypto", entry=1.0, stop_loss=0.9, take_profit=1.2, sample_size=999,
                               expected_value_r=1.0, rationale={}, bar_time=0, created_at=now())
        with session_scope() as s:
            ok, checks, _ = rm._evaluate(s, fake, _acct(), {"halt": {"active": False}, "daily_pct": 0,
                                                             "drawdown_pct": 0}, 0.0, set(), 0)
        ks_check = next(c for c in checks if c["key"] == "kill_switch")
        passed = (not ok) and (not ks_check["ok"])
    finally:
        with session_scope() as s:
            if kill_switch_state(s).get("reason") == "Test der Notabschaltung":
                set_state(s, "kill_switch", before if before else {"active": False})
    res = {"ok": passed, "at": now(), "detail": "Veto bei aktivem Kill Switch bestätigt" if passed else "Kein Veto!",
           "by": user}
    with session_scope() as s:
        set_state(s, "killswitch_test", res)
    return res


class StrategyBody(BaseModel):
    enabled: bool
    reason: str = ""


@app.post("/api/strategies/{name}")
def set_strategy(name: str, body: StrategyBody, user=Depends(require_auth)):
    if name not in STRATEGIES:
        raise HTTPException(404)
    with session_scope() as s:
        r = s.get(StrategyState, name) or StrategyState(name=name, stats={})
        s.add(r)
        r.enabled = body.enabled
        r.disabled_by = None if body.enabled else "user"
        r.disabled_reason = None if body.enabled else (body.reason or f"manuell deaktiviert durch {user}")
        r.updated_at = now()
        s.add(AgentLog(agent="performance", level="INFO",
                       message=f"Strategie {name} {'aktiviert' if body.enabled else 'deaktiviert'} durch {user}",
                       data={}, ts=now()))
        return {"name": name, "enabled": r.enabled}


@app.post("/api/halt/reset")
def reset_halt(user=Depends(require_auth)):
    acct = _acct()
    with session_scope() as s:
        acc = get_account(s, acct)
        snap = account_snapshot(s, acc)
        acc.peak_equity = snap["equity"]  # new drawdown reference after a conscious reset
        set_state(s, halt_key(acct), {"active": False, "reason": f"manuell freigegeben durch {user}", "at": now()})
        s.add(AgentLog(agent="risk", level="WARNING", message=f"Handelsstopp manuell aufgehoben durch {user}",
                       data={}, ts=now()))
    return {"ok": True}


class LiveConfirmBody(BaseModel):
    regulatory_checked: bool = False
    note: str = ""


@app.post("/api/live-gate/confirm")
def live_confirm(body: LiveConfirmBody, user=Depends(require_auth)):
    with session_scope() as s:
        cur = get_state(s, "live_owner_confirmations")
        cur.update({"regulatory_checked": body.regulatory_checked, "regulatory_note": body.note or "—", "by": user,
                    "at": now()})
        set_state(s, "live_owner_confirmations", cur)
        return live_gate_status(s)


@app.post("/api/selftest")
def run_selftest_ep(user=Depends(require_auth)):
    """Runs the controlled integration test in a separate process (own simulated clock)."""
    with session_scope() as s:
        r = get_state(s, "selftest_running")
        if r.get("running") and now() - r.get("at", 0) < 600:
            return {"started": False, "message": "Selbsttest läuft bereits"}
        set_state(s, "selftest_running", {"running": True, "at": now(), "by": user})

    def _run():
        try:
            subprocess.run([sys.executable, "-m", "app.selftest"], cwd=str(Path(__file__).resolve().parents[2]),
                           env=os.environ.copy(), timeout=900, capture_output=True)
        finally:
            with session_scope() as s:
                set_state(s, "selftest_running", {"running": False, "at": now()})

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True}


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled API error on %s", request.url.path)
    return JSONResponse({"detail": f"interner Fehler: {type(exc).__name__}"}, status_code=500)
