"""Integration: controlled self-test, full pipeline on synthetic data, worker, API, performance agent."""
import time

import pytest

from app import clock
from app.agents.performance import PerformanceAnalyst, trade_metrics
from app.config import reset_settings_cache
from app.db import AgentState, Cycle, Position, StrategyState, get_state, session_scope


def test_controlled_integration_selftest_triggers_order():
    from app.selftest import run_selftest

    res = run_selftest(store=False)
    failed = [c for c in res["checks"] if not c["ok"]]
    assert res["ok"], failed
    with session_scope() as s:
        pos = s.query(Position).filter(Position.account == "SELFTEST").all()
        assert pos and all(p.is_test for p in pos)
        assert s.query(Position).filter(Position.account == "PAPER").count() == 0
        # agent rows of the self-test are isolated
        assert s.get(AgentState, "scanner") is None and s.get(AgentState, "selftest:scanner") is not None


def test_full_pipeline_synthetic_16h(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_MODE", "synthetic")
    reset_settings_cache()
    from app.data.hub import build_market_data
    from app.engine.orchestrator import Orchestrator

    ref = [1_790_000_000.0]
    clock.set_clock(ref)
    md = build_market_data()
    orch = Orchestrator(md)
    assert orch.account == "TEST" and orch.is_test
    for _ in range(64):  # 16 h, cycle every 15 min, monitor every minute
        r = orch.run_cycle()
        assert r["status"] == "ok", r
        for _ in range(15):
            ref[0] += 60
            orch.run_monitor()
    orch.run_performance()
    with session_scope() as s:
        cycles = s.query(Cycle).all()
        assert len(cycles) == 64 and all(c.status == "ok" for c in cycles)
        assert all(len(c.steps) == 7 and all(v["ok"] for v in c.steps.values()) for c in cycles)
        no_trade = [c for c in cycles if c.executed == 0]
        assert all(c.no_trade_reason for c in no_trade)  # every idle cycle documents why
        pos = s.query(Position).all()
        assert all(p.is_test and p.account == "TEST" for p in pos)
        from app.engine.portfolio import account_snapshot, get_account

        acc = get_account(s, "TEST")
        snap = account_snapshot(s, acc)
        realized = sum(p.pnl_eur for p in pos if p.status == "closed")
        open_fees = sum(p.entry_fee_eur for p in pos if p.status == "open")
        assert abs(snap["equity"] - (acc.starting_capital + realized - open_fees + snap["unrealized"])) < 1e-6


def test_worker_runs_jobs_in_background_and_holds_single_lease(monkeypatch):
    monkeypatch.setenv("MARKET_DATA_MODE", "synthetic")
    monkeypatch.setenv("CYCLE_INTERVAL_SEC", "2")
    monkeypatch.setenv("MONITOR_INTERVAL_SEC", "1")
    reset_settings_cache()
    import threading

    from app.worker import Worker

    w1, w2 = Worker(), Worker()
    t1 = threading.Thread(target=w1.run, daemon=True)
    t1.start()
    time.sleep(1.5)
    t2 = threading.Thread(target=w2.run, daemon=True)
    t2.start()
    deadline = time.time() + 40
    while time.time() < deadline:
        with session_scope() as s:
            n = s.query(Cycle).filter(Cycle.status == "ok").count()
        if n >= 2:
            break
        time.sleep(0.5)
    w1.stop.set()
    w2.stop.set()
    t1.join(30)
    t2.join(30)
    assert n >= 2
    assert w1.is_leader and not w2.is_leader
    with session_scope() as s:
        hb = get_state(s, "worker_heartbeat")
        assert hb["jobs"]["cycle"]["runs"] >= 1 and hb["jobs"]["monitor"]["runs"] >= 1


def _closed(strategy, pnl, r, t):
    return Position(account="PAPER", client_order_id=f"c{t}", symbol="X", asset_class="crypto", strategy=strategy,
                    side="long", timeframe="1h", qty=1, quote_ccy="USD", fx_entry=1, entry_price=100, entry_ref_price=100,
                    entry_time=t, stop_loss=99, take_profit=102, initial_risk_eur=10, entry_fee_eur=0.1, max_hold_sec=3600,
                    status="closed", exit_price=100, exit_time=t + 600, exit_reason="Stop-Loss erreicht" if pnl < 0 else
                    "Take-Profit erreicht", exit_fee_eur=0.1, gross_pnl_eur=pnl + 0.2, pnl_eur=pnl, r_multiple=r,
                    slippage_eur=0.05, is_test=False)


def test_metrics_and_auto_disable():
    trades = [_closed("scalping", -10, -1, i * 1000) for i in range(18)] + [_closed("scalping", 5, 0.5, 99_000 + i)
                                                                           for i in range(4)]
    m = trade_metrics(trades, 10_000)
    assert m["trades"] == 22 and m["wins"] == 4
    assert abs(m["profit_factor"] - 20 / 180) < 1e-9
    assert abs(m["max_drawdown_eur"] - 180) < 1e-9
    with session_scope() as s:
        s.add_all(trades)
        s.add(_closed("breakout", 30, 3, 5))
    PerformanceAnalyst().run("PAPER")
    with session_scope() as s:
        assert s.get(StrategyState, "scalping").enabled is False
        assert "Profit Factor" in s.get(StrategyState, "scalping").disabled_reason
        assert s.get(StrategyState, "breakout") is None or s.get(StrategyState, "breakout").enabled


def test_test_trades_never_count_as_performance():
    with session_scope() as s:
        p = _closed("breakout", 500, 5, 1)
        p.is_test = True
        s.add(p)
    res = PerformanceAnalyst().run("PAPER")
    assert res["trades"] == 0


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app) as c:
        yield c


def test_api_auth_and_endpoints(client):
    assert client.get("/api/overview").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/healthz").json() == {"ok": True}
    a = ("admin", "secret")
    for ep in ["/api/overview", "/api/health", "/api/cycles", "/api/scanner", "/api/agents", "/api/logs",
               "/api/signals", "/api/positions", "/api/positions?status=closed", "/api/orders", "/api/performance",
               "/api/strategies", "/api/live-gate", "/api/selftest", "/", "/static/app.js", "/static/chart.js"]:
        assert client.get(ep, auth=a).status_code == 200, ep
    assert client.get("/static/../config.py", auth=a).status_code == 404
    h = client.get("/api/health", auth=a).json()
    assert h["overall"] == "red"  # no worker running -> must NOT be green
    assert any(c["name"] == "Hintergrund-Worker" and c["status"] == "red" for c in h["components"])


def test_api_controls(client):
    a = ("admin", "secret")
    r = client.post("/api/kill-switch/test", auth=a).json()
    assert r["ok"]
    assert client.post("/api/kill-switch", json={"active": True, "reason": "x"}, auth=a).json()["active"]
    assert client.get("/api/overview", auth=a).json()["kill_switch"]["active"]
    client.post("/api/kill-switch", json={"active": False}, auth=a)
    assert client.post("/api/strategies/momentum", json={"enabled": False}, auth=a).json()["enabled"] is False
    gate = client.get("/api/live-gate", auth=a).json()
    assert gate["live_allowed"] is False and gate["live_active"] is False


def test_api_refuses_without_password_in_production(monkeypatch, client):
    monkeypatch.delenv("DASHBOARD_PASSWORD")
    reset_settings_cache()
    assert client.get("/api/overview", auth=("admin", "")).status_code == 503


def test_live_broker_cannot_be_constructed():
    from app.brokers.live import LiveBroker, LiveTradingDisabled

    with session_scope() as s, pytest.raises(LiveTradingDisabled):
        LiveBroker(s)
