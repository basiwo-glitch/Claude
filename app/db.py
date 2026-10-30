"""Database models and session handling (PostgreSQL in production, SQLite for dev/tests).

All timestamps are stored as UTC epoch seconds (float) to stay portable.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app import clock as _clock
from app.config import get_settings


def now() -> float:
    return _clock.now()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)  # PAPER | TEST | LIVE
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    starting_capital: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    peak_equity: Mapped[float] = mapped_column(Float)
    day_start_equity: Mapped[float] = mapped_column(Float)
    day_start_date: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Cycle(Base):
    """One full pass of the pipeline (scan -> analyse -> signal -> risk -> execute)."""

    __tablename__ = "cycles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|error|timeout
    markets_scanned: Mapped[int] = mapped_column(Integer, default=0)
    candidates: Mapped[int] = mapped_column(Integer, default=0)
    analysed: Mapped[int] = mapped_column(Integer, default=0)
    signals: Mapped[int] = mapped_column(Integer, default=0)
    approved: Mapped[int] = mapped_column(Integer, default=0)
    executed: Mapped[int] = mapped_column(Integer, default=0)
    no_trade_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[dict] = mapped_column(JSON, default=dict)  # per step timing/status = proof of work


class ScanResult(Base):
    __tablename__ = "scan_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    asset_class: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    change_pct: Mapped[float] = mapped_column(Float)
    quote_volume: Mapped[float] = mapped_column(Float)
    volume_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    move_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend: Mapped[str | None] = mapped_column(String(8), nullable=True)
    score: Mapped[float] = mapped_column(Float)
    rank: Mapped[int] = mapped_column(Integer)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    signal: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_time: Mapped[float] = mapped_column(Float)


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[float] = mapped_column(Float, default=now)
    summary: Mapped[dict] = mapped_column(JSON)  # per timeframe indicators, patterns, levels


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signal_dedupe", "account", "symbol", "strategy", "side", "bar_time"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    account: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(5))  # long|short
    timeframe: Mapped[str] = mapped_column(String(4))
    bar_time: Mapped[float] = mapped_column(Float, default=0.0)  # open time of the signal bar
    entry: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    prob_ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    prob_ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    expected_value_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    reward_risk: Mapped[float] = mapped_column(Float)
    cost_r: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="proposed")  # proposed|approved|vetoed|executed|failed
    risk_notes: Mapped[dict] = mapped_column(JSON, default=dict)
    veto_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_position_client_order"),
        Index("ix_pos_account_status", "account", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(32))
    client_order_id: Mapped[str] = mapped_column(String(80))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(5))
    timeframe: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    quote_ccy: Mapped[str] = mapped_column(String(8))  # USD / EUR
    fx_entry: Mapped[float] = mapped_column(Float)  # EUR per 1 unit of quote ccy
    entry_price: Mapped[float] = mapped_column(Float)  # fill price incl. slippage
    entry_ref_price: Mapped[float] = mapped_column(Float)  # market price before slippage
    entry_time: Mapped[float] = mapped_column(Float, default=now)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    initial_risk_eur: Mapped[float] = mapped_column(Float)
    entry_fee_eur: Mapped[float] = mapped_column(Float)
    max_hold_sec: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default="open")  # open|closed
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_price_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_candle: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_ref_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_fee_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    fx_exit: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_pnl_eur: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_eur: Mapped[float | None] = mapped_column(Float, nullable=True)  # net of fees & slippage
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    r_multiple: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_eur: Mapped[float] = mapped_column(Float, default=0.0)
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)
    exit_rationale: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base):
    """Every simulated fill (entry and exit). The unique client_order_id prevents duplicates."""

    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("client_order_id", name="uq_order_client_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(32), index=True)
    client_order_id: Mapped[str] = mapped_column(String(96))
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))  # buy|sell
    kind: Mapped[str] = mapped_column(String(8))  # entry|exit
    qty: Mapped[float] = mapped_column(Float)
    ref_price: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float] = mapped_column(Float)
    fee_eur: Mapped[float] = mapped_column(Float)
    slippage_bps: Mapped[float] = mapped_column(Float)
    broker: Mapped[str] = mapped_column(String(16), default="paper")
    reason: Mapped[str] = mapped_column(Text)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[float] = mapped_column(Float, default=now, index=True)
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    unrealized: Mapped[float] = mapped_column(Float)
    gross_exposure: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)


class AgentState(Base):
    __tablename__ = "agent_state"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(12), default="idle")  # idle|working|ok|warning|error|halted
    current_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_activity: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_success: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_result: Mapped[dict] = mapped_column(JSON, default=dict)
    runs: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)


class AgentLog(Base):
    """Decision / event log of every agent (what it did and why)."""

    __tablename__ = "agent_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=now, index=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(8), default="INFO")
    cycle_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class StrategyState(Base):
    __tablename__ = "strategy_state"
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(String(16), nullable=True)  # user|performance
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemState(Base):
    """Key/value store: kill switch, halt reasons, worker lease, provider health ..."""

    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[float] = mapped_column(Float, default=now)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    body: Mapped[dict] = mapped_column(JSON)


# ---------------------------------------------------------------------------
_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _make_engine(url: str) -> Engine:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, future=True)

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return eng
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, future=True)


def init_db(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    _engine = _make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    Base.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_db()
    assert _engine is not None
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_db()
    assert _SessionLocal is not None
    s: Session = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def db_ping() -> bool:
    with get_engine().connect() as c:
        c.execute(text("SELECT 1"))
    return True


def get_state(s: Session, key: str, default: dict | None = None) -> dict:
    row = s.get(SystemState, key)
    return dict(row.value) if row and row.value is not None else (default or {})


def set_state(s: Session, key: str, value: dict) -> None:
    row = s.get(SystemState, key)
    if row is None:
        s.add(SystemState(key=key, value=value, updated_at=now()))
    else:
        row.value = value
        row.updated_at = now()
