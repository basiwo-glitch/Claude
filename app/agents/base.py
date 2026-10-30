"""Agent base: every agent reports status, current task, results and decisions to the DB.

The dashboard's status lights are derived from these rows: an agent is only shown
green if it finished successfully recently (see app/api/health.py).
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from contextlib import contextmanager

from app.db import AgentLog, AgentState, now, session_scope

AGENTS = {
    "scanner": {"title": "Market Scanner", "no": 1},
    "technical": {"title": "Technical Analyst", "no": 2},
    "strategist": {"title": "AI Strategy Analyst", "no": 3},
    "risk": {"title": "Risk Manager", "no": 4},
    "execution": {"title": "Execution Agent", "no": 5},
    "performance": {"title": "Performance Analyst", "no": 6},
}


class Agent:
    name = "base"

    def __init__(self, prefix: str = "") -> None:
        # prefix != "" isolates status rows/logs (used by the self-test: "selftest:scanner")
        self.state_name = f"{prefix}{self.name}"
        self.log = logging.getLogger(f"agent.{self.state_name}")
        self._tl = threading.local()  # per-thread: cycle and monitor jobs share agent objects

    # thread-local attributes ------------------------------------------------------
    @property
    def cycle_id(self) -> int | None:
        return getattr(self._tl, "cycle_id", None)

    @cycle_id.setter
    def cycle_id(self, v: int | None) -> None:
        self._tl.cycle_id = v

    @property
    def _pending(self) -> list:
        if not hasattr(self._tl, "pending"):
            self._tl.pending = []
        return self._tl.pending

    @_pending.setter
    def _pending(self, v: list) -> None:
        self._tl.pending = v

    @property
    def _in_task(self) -> bool:
        return getattr(self._tl, "in_task", False)

    @_in_task.setter
    def _in_task(self, v: bool) -> None:
        self._tl.in_task = v

    # -- state ------------------------------------------------------------------
    def _state(self, **fields) -> None:
        try:
            with session_scope() as s:
                row = s.get(AgentState, self.state_name)
                if row is None:
                    row = AgentState(name=self.state_name, last_result={})
                    s.add(row)
                for k, v in fields.items():
                    setattr(row, k, v)
        except Exception:  # never let status reporting break trading logic
            self.log.exception("could not update agent state")

    def decision(self, message: str, data: dict | None = None, level: str = "INFO") -> None:
        """Record a decision / observation (shown in the Agent Center).

        Entries are buffered and written by ``flush()`` (automatically at the end of
        every ``task``) so that no second DB transaction is opened while the caller
        still holds one (SQLite allows only a single writer).
        """
        getattr(self.log, level.lower(), self.log.info)(message)
        self._pending.append(AgentLog(agent=self.state_name, level=level, cycle_id=self.cycle_id, message=message[:2000],
                                      data=data or {}, ts=now()))

    def flush(self) -> None:
        if not self._pending:
            return
        items, self._pending = self._pending, []
        try:
            with session_scope() as s:
                s.add_all(items)
        except Exception:
            self.log.exception("could not write agent log")

    @contextmanager
    def task(self, description: str):
        """Wrap a unit of work: status=working -> ok / error."""
        t0 = time.time()
        self._state(status="working", current_task=description, last_activity=now())
        outer = self._in_task
        self._in_task = True
        result: dict = {}
        try:
            try:
                yield result
            finally:
                self._in_task = outer
        except Exception as e:
            tb = traceback.format_exc(limit=6)
            self.log.error("%s failed: %s\n%s", description, e, tb)
            self.flush()
            with session_scope() as s:
                row = s.get(AgentState, self.state_name)
                if row is None:
                    row = AgentState(name=self.state_name, last_result={})
                    s.add(row)
                row.status = "error"
                row.current_task = None
                row.last_activity = now()
                row.last_error = f"{type(e).__name__}: {e}"[:1000]
                row.last_error_at = now()
                row.errors = (row.errors or 0) + 1
                row.runs = (row.runs or 0) + 1
                s.add(AgentLog(agent=self.state_name, level="ERROR", cycle_id=self.cycle_id,
                               message=f"{description} fehlgeschlagen: {type(e).__name__}: {e}"[:2000],
                               data={"traceback": tb[-3000:]}, ts=now()))
            raise
        else:
            self.flush()
            result.setdefault("duration_sec", round(time.time() - t0, 2))
            with session_scope() as s:
                row = s.get(AgentState, self.state_name)
                if row is None:
                    row = AgentState(name=self.state_name)
                    s.add(row)
                row.status = result.pop("_status", "ok")
                row.current_task = None
                row.last_activity = now()
                row.last_success = now()
                row.last_result = result
                row.runs = (row.runs or 0) + 1
