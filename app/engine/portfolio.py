"""Paper account bookkeeping (all amounts in EUR)."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Account, EquitySnapshot, Position, now

TZ = ZoneInfo("Europe/Berlin")

# Serialises every cash-changing operation inside the (single, lease-holding) worker
# process; entries (cycle thread) and exits (monitor thread) never race.
import threading  # noqa: E402

ACCOUNT_LOCK = threading.RLock()


def today_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now(), TZ).strftime("%Y-%m-%d")


def get_account(s: Session, name: str) -> Account:
    acc = s.query(Account).filter(Account.name == name).one_or_none()
    if acc is None:
        cap = get_settings().starting_capital
        acc = Account(name=name, currency="EUR", starting_capital=cap, cash=cap, peak_equity=cap,
                      day_start_equity=cap, day_start_date=today_str(), created_at=now())
        s.add(acc)
        s.flush()
    return acc


def position_value_eur(p: Position, price: float | None = None, fx: float | None = None) -> float:
    """Signed market value: + for longs, - for shorts (short = liability)."""
    px = price if price is not None else (p.last_price or p.entry_price)
    f = fx if fx is not None else p.fx_entry
    v = p.qty * px * f
    return v if p.side == "long" else -v


def unrealized_pnl_eur(p: Position, price: float | None = None, fx: float | None = None) -> float:
    px = price if price is not None else (p.last_price or p.entry_price)
    f = fx if fx is not None else p.fx_entry
    if p.side == "long":
        return p.qty * (px * f - p.entry_price * p.fx_entry)
    return p.qty * (p.entry_price * p.fx_entry - px * f)


def account_snapshot(s: Session, acc: Account) -> dict:
    opens = s.query(Position).filter(Position.account == acc.name, Position.status == "open").all()
    mv = sum(position_value_eur(p) for p in opens)
    gross = sum(abs(position_value_eur(p)) for p in opens)
    unreal = sum(unrealized_pnl_eur(p) for p in opens)
    equity = acc.cash + mv
    return {"equity": equity, "cash": acc.cash, "unrealized": unreal, "gross_exposure": gross,
            "open_positions": len(opens), "positions": opens}


def roll_day(s: Session, acc: Account, equity: float) -> None:
    d = today_str()
    if acc.day_start_date != d:
        acc.day_start_date = d
        acc.day_start_equity = equity
    if equity > acc.peak_equity:
        acc.peak_equity = equity


def record_equity(s: Session, acc: Account, min_interval: float = 55.0, force: bool = False) -> dict:
    snap = account_snapshot(s, acc)
    roll_day(s, acc, snap["equity"])
    last = (s.query(EquitySnapshot).filter(EquitySnapshot.account == acc.name)
            .order_by(EquitySnapshot.ts.desc()).first())
    if force or last is None or now() - last.ts >= min_interval:
        s.add(EquitySnapshot(account=acc.name, ts=now(), equity=snap["equity"], cash=snap["cash"],
                             unrealized=snap["unrealized"], gross_exposure=snap["gross_exposure"],
                             open_positions=snap["open_positions"]))
    return snap
