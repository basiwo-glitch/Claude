"""Risk manager, paper execution, exits and P&L accounting."""
import pytest

from app import clock
from app.agents.execution import ExecutionAgent
from app.agents.risk import RiskManager, set_kill_switch
from app.brokers.paper import DuplicateOrder, PaperBroker
from app.config import get_settings
from app.db import Account, Order, Position, Signal, get_state, session_scope
from app.engine.portfolio import account_snapshot, get_account
from tests.helpers import fake_md, make_signal

T0 = 1_750_000_000.0


@pytest.fixture
def env():
    ref = [T0]
    clock.set_clock(ref)
    md, p = fake_md(fx=0.9)
    p.set_price("AAAUSDT", 100.0)
    return ref, md, p


def review(md, ids, account="PAPER"):
    rm = RiskManager(md)
    approved, res = rm.review(1, ids, account)
    return approved, res


def test_risk_approves_and_sizes_by_risk_budget(env):
    _, md, _ = env
    sid = make_signal()
    approved, _ = review(md, [sid])
    assert approved == [sid]
    with session_scope() as s:
        g = s.get(Signal, sid)
        sz = g.risk_notes["sizing"]
        st = get_settings()
        # risk per trade = 0.5 % of 10 000 = 50 EUR; never above the budget
        assert sz["risk_eur"] <= 10_000 * st.risk_per_trade_pct / 100 + 1e-6
        assert sz["notional_eur"] <= 10_000 * st.max_position_pct / 100 + 1e-6
        assert all(c["ok"] for c in g.risk_notes["checks"])


@pytest.mark.parametrize("kw,key", [
    ({"n": 5}, "samples"),
    ({"ev": -0.1}, "ev"),
    ({"tp": 101.0}, "rr"),
    ({"entry": 103.0, "sl": 101.0, "tp": 107.0}, "drift"),
])
def test_risk_vetoes(env, kw, key):
    _, md, _ = env
    sid = make_signal(**kw)
    approved, _ = review(md, [sid])
    assert approved == []
    with session_scope() as s:
        g = s.get(Signal, sid)
        assert g.status == "vetoed"
        assert key in [c["key"] for c in g.risk_notes["checks"] if not c["ok"]]


def test_kill_switch_and_stale_data_veto(env):
    ref, md, p = env
    with session_scope() as s:
        set_kill_switch(s, True, "test", "pytest")
    sid = make_signal()
    assert review(md, [sid])[0] == []
    with session_scope() as s:
        set_kill_switch(s, False, "", "pytest")
    p.set_price("AAAUSDT", 100.0, t=T0 - 3600)  # one hour old quote
    sid = make_signal(bar_time=2000)
    assert review(md, [sid])[0] == []
    with session_scope() as s:
        assert "fresh" in [c["key"] for c in s.get(Signal, sid).risk_notes["checks"] if not c["ok"]]


def test_market_closed_veto(env):
    _, md, p = env
    p.open = False
    sid = make_signal()
    assert review(md, [sid])[0] == []


def open_position(md, side="long", entry=100.0, sl=98.0, tp=104.0):
    sid = make_signal(side=side, entry=entry, sl=sl, tp=tp)
    approved, _ = review(md, [sid])
    assert approved == [sid]
    ex = ExecutionAgent(md)
    pids, _ = ex.execute(1, approved, "PAPER")
    assert len(pids) == 1
    return ex, pids[0]


def test_execution_long_take_profit_pnl_and_cash(env):
    ref, md, p = env
    ex, pid = open_position(md)
    with session_scope() as s:
        pos = s.get(Position, pid)
        assert pos.entry_price > 100.0  # adverse slippage on buy
        qty, entry, fee_in = pos.qty, pos.entry_price, pos.entry_fee_eur
        acc = get_account(s, "PAPER")
        assert abs(acc.cash - (10_000 - qty * entry * 0.9 - fee_in)) < 1e-6
    # next minute bar reaches the target
    p.add_bar("AAAUSDT", T0 + 60, 100.5, 104.5, 100.2, 104.2)
    p.set_price("AAAUSDT", 104.2)
    ref[0] = T0 + 125
    ex.monitor("PAPER")
    with session_scope() as s:
        pos = s.get(Position, pid)
        assert pos.status == "closed" and pos.exit_reason == "Take-Profit erreicht"
        assert pos.exit_price == 104.0  # limit fill, no slippage
        fee_out = pos.exit_fee_eur
        expected = qty * (104.0 * 0.9 - entry * 0.9) - fee_in - fee_out
        assert abs(pos.pnl_eur - expected) < 1e-9
        assert abs(get_account(s, "PAPER").cash - (10_000 + expected)) < 1e-6
        assert s.query(Order).filter(Order.position_id == pid).count() == 2


def test_execution_short_stop_loss_same_bar_conservative(env):
    ref, md, p = env
    ex, pid = open_position(md, side="short", entry=100.0, sl=102.0, tp=96.0)
    # one bar touches BOTH target (95) and stop (103) -> stop is assumed first
    p.add_bar("AAAUSDT", T0 + 60, 100.0, 103.0, 95.0, 99.0)
    p.set_price("AAAUSDT", 99.0)
    ref[0] = T0 + 130
    ex.monitor("PAPER")
    with session_scope() as s:
        pos = s.get(Position, pid)
        assert pos.exit_reason == "Stop-Loss erreicht"
        assert pos.exit_price > 102.0  # stop + adverse slippage on the buy-back
        assert pos.pnl_eur < 0
        expected = pos.qty * (pos.entry_price * 0.9 - pos.exit_price * 0.9) - pos.entry_fee_eur - pos.exit_fee_eur
        assert abs(pos.pnl_eur - expected) < 1e-9


def test_gap_through_stop_fills_at_open(env):
    ref, md, p = env
    ex, pid = open_position(md)
    p.add_bar("AAAUSDT", T0 + 60, 96.0, 96.5, 95.5, 96.2)  # opens below the stop at 98
    p.set_price("AAAUSDT", 96.2)
    ref[0] = T0 + 130
    ex.monitor("PAPER")
    with session_scope() as s:
        pos = s.get(Position, pid)
        assert pos.exit_ref_price == 96.0 and pos.exit_price < 96.0


def test_time_exit(env):
    ref, md, p = env
    ex, pid = open_position(md)
    with session_scope() as s:
        hold = s.get(Position, pid).max_hold_sec
    p.set_price("AAAUSDT", 100.5, t=T0 + hold + 10)
    ref[0] = T0 + hold + 10
    ex.monitor("PAPER")
    with session_scope() as s:
        assert s.get(Position, pid).exit_reason == "Maximale Haltedauer erreicht"


def test_duplicate_orders_prevented(env):
    _, md, _ = env
    ex, pid = open_position(md)
    # same signal bar again -> risk manager vetoes (idempotency + open position) ...
    sid2 = make_signal()
    assert review(md, [sid2])[0] == []
    # ... and the broker itself refuses a second order with the same client id
    with session_scope() as s:
        pos = s.get(Position, pid)
        g = s.get(Signal, sid2)
        with pytest.raises(DuplicateOrder):
            PaperBroker().open_position(s, get_account(s, "PAPER"), client_order_id=pos.client_order_id, signal=g,
                                        qty=1, ref_price=100, fx=0.9, quote_ccy="USD", quote_volume=1e9,
                                        max_hold_sec=3600, reason="dup", is_test=False)


def test_equity_identity(env):
    ref, md, p = env
    ex, pid = open_position(md)
    p.set_price("AAAUSDT", 101.0)
    ref[0] = T0 + 30
    ex.monitor("PAPER")
    with session_scope() as s:
        acc = get_account(s, "PAPER")
        snap = account_snapshot(s, acc)
        pos = s.get(Position, pid)
        assert abs(snap["equity"] - (10_000 - pos.entry_fee_eur + snap["unrealized"])) < 1e-6


def test_daily_loss_halt_and_drawdown_kill_switch(env):
    _, md, _ = env
    rm = RiskManager(md)
    with session_scope() as s:
        acc = get_account(s, "PAPER")
        acc.cash = 9_650.0  # -3.5 % today
    res = rm.check_account_limits("PAPER")
    assert res["halt"]["active"] and res["halt"]["kind"] == "daily_loss"
    sid = make_signal()
    assert review(md, [sid])[0] == []
    # drawdown beyond the limit -> persistent halt + kill switch
    with session_scope() as s:
        from app.agents.risk import halt_key
        from app.db import set_state

        set_state(s, halt_key("PAPER"), {"active": False})
        acc = get_account(s, "PAPER")
        acc.cash = 8_400.0
        acc.day_start_equity = 8_400.0
    res = rm.check_account_limits("PAPER")
    assert res["halt"]["kind"] == "drawdown"
    with session_scope() as s:
        assert get_state(s, "kill_switch")["active"]


def test_consecutive_errors_trigger_kill_switch(env):
    _, md, _ = env
    rm = RiskManager(md)
    for _ in range(get_settings().max_consecutive_errors):
        rm.register_cycle_result(False, "PAPER")
    with session_scope() as s:
        assert get_state(s, "kill_switch")["active"]


def test_kill_switch_flatten_closes_positions(env):
    ref, md, p = env
    ex, pid = open_position(md)
    with session_scope() as s:
        set_kill_switch(s, True, "flatten test", "pytest", flatten=True)
    ref[0] = T0 + 20
    p.set_price("AAAUSDT", 100.3)
    ex.monitor("PAPER")
    with session_scope() as s:
        assert s.get(Position, pid).exit_reason == "Kill Switch: Glattstellung"
        assert s.query(Account).count() == 1
