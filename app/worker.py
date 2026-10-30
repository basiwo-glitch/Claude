"""Background worker: runs the trading pipeline 24/7 without any browser.

* single active worker guaranteed by a DB lease (a second instance waits)
* jobs: full cycle (default every 5 min), position monitor (30 s), performance (15 min)
* heartbeat + per-job status written to the DB (dashboard "green" only if fresh)
* watchdog: if the main loop or a job hangs, the process exits with code 3 and the
  container/systemd restart policy brings it back up
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid

from app.config import get_settings
from app.data.hub import build_market_data
from app.db import get_state, init_db, now, session_scope, set_state
from app.engine.orchestrator import Orchestrator
from app.logging_setup import setup_logging

log = logging.getLogger("worker")

LEASE_KEY = "worker_lease"
HEARTBEAT_KEY = "worker_heartbeat"
LEASE_TTL = 90.0


class Job:
    def __init__(self, name: str, interval: float, fn, timeout: float):
        self.name = name
        self.interval = interval
        self.fn = fn
        self.timeout = timeout
        self.next_run = 0.0
        self.thread: threading.Thread | None = None
        self.started_at: float | None = None
        self.last = {"status": "never", "last_start": None, "last_end": None, "duration": None, "error": None,
                     "runs": 0, "failures": 0, "result": None}

    def due(self, t: float) -> bool:
        return t >= self.next_run and (self.thread is None or not self.thread.is_alive())

    def start(self) -> None:
        self.started_at = time.time()
        self.next_run = self.started_at + self.interval
        self.last["last_start"] = now()
        self.last["status"] = "running"
        self.thread = threading.Thread(target=self._run, name=f"job-{self.name}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        t0 = time.time()
        try:
            res = self.fn()
            ok = not (isinstance(res, dict) and res.get("status") == "error")
            self.last.update(status="ok" if ok else "error", error=None if ok else res.get("error"),
                             result=_short(res))
            if not ok:
                self.last["failures"] += 1
        except Exception as e:  # noqa: BLE001
            log.error("job %s crashed: %s\n%s", self.name, e, traceback.format_exc(limit=8))
            self.last.update(status="error", error=f"{type(e).__name__}: {e}"[:500])
            self.last["failures"] += 1
        finally:
            self.last["runs"] += 1
            self.last["last_end"] = now()
            self.last["duration"] = round(time.time() - t0, 2)
            self.started_at = None

    def overdue(self) -> float:
        return (time.time() - self.started_at - self.timeout) if self.started_at else 0.0


def _short(res):
    if not isinstance(res, dict):
        return None
    keep = ("status", "markets_scanned", "analysed", "signals", "approved", "executed", "closed", "no_trade_reason",
            "open_checked", "equity", "trades", "error")
    return {k: res[k] for k in keep if k in res}


class Worker:
    def __init__(self) -> None:
        self.st = get_settings()
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = threading.Event()
        self.last_tick = time.time()
        self.is_leader = False
        self.started = now()
        self.md = build_market_data(self.st)
        self.orch = Orchestrator(self.md)
        st = self.st
        self.jobs = [
            Job("cycle", st.cycle_interval_sec, self.orch.run_cycle, st.cycle_timeout_sec),
            Job("monitor", st.monitor_interval_sec, self.orch.run_monitor, 120),
            Job("performance", st.performance_interval_sec, self.orch.run_performance, 300),
        ]
        # first cycle shortly after start, monitor first
        self.jobs[0].next_run = time.time() + 3

    # -- lease ------------------------------------------------------------------------------
    def acquire_lease(self) -> bool:
        from app.db import SystemState

        with session_scope() as s:
            q = s.query(SystemState).filter(SystemState.key == LEASE_KEY)
            if s.bind.dialect.name == "postgresql":
                q = q.with_for_update()
            row = q.one_or_none()
            val = dict(row.value) if row else {}
            t = now()
            if not val or val.get("owner") == self.owner or val.get("expires", 0) < t:
                new = {"owner": self.owner, "expires": t + LEASE_TTL, "since": val.get("since", t)
                       if val.get("owner") == self.owner else t}
                set_state(s, LEASE_KEY, new)
                if not self.is_leader:
                    log.info("worker lease acquired by %s", self.owner)
                self.is_leader = True
                return True
        if self.is_leader:
            log.warning("worker lease lost")
        self.is_leader = False
        return False

    def heartbeat(self) -> None:
        with session_scope() as s:
            set_state(s, HEARTBEAT_KEY, {
                "ts": now(), "owner": self.owner, "leader": self.is_leader, "started_at": self.started,
                "pid": os.getpid(), "host": socket.gethostname(), "mode": self.st.data_mode,
                "account": self.st.account_name,
                "jobs": {j.name: {**j.last, "interval": j.interval, "next_run": j.next_run if self.is_leader else None,
                                  "overdue_sec": round(j.overdue(), 1)} for j in self.jobs},
                "market_data": self.md.health(),
            })

    # -- main loop ---------------------------------------------------------------------------
    def watchdog(self) -> None:
        while not self.stop.wait(10):
            stuck = time.time() - self.last_tick
            worst = max((j.overdue() for j in self.jobs), default=0)
            if stuck > 180 or worst > max(j.timeout for j in self.jobs) * 2:
                log.critical("WATCHDOG: main loop stuck %.0fs / job overdue %.0fs -> exiting for restart", stuck, worst)
                logging.shutdown()
                os._exit(3)

    def run(self) -> None:
        threading.Thread(target=self.watchdog, name="watchdog", daemon=True).start()
        last_hb = 0.0
        last_lease = 0.0
        while not self.stop.is_set():
            self.last_tick = time.time()
            try:
                if time.time() - last_lease > 20:
                    self.acquire_lease()
                    last_lease = time.time()
                if self.is_leader:
                    for j in self.jobs:
                        if j.due(time.time()):
                            j.start()
                if time.time() - last_hb > 5:
                    self.heartbeat()
                    last_hb = time.time()
            except Exception as e:  # noqa: BLE001  (DB outage etc.) -> keep running, retry
                log.error("worker loop error: %s", e)
                time.sleep(5)
            self.stop.wait(1.0)
        log.info("worker stopping; waiting for running jobs")
        for j in self.jobs:
            if j.thread and j.thread.is_alive():
                j.thread.join(timeout=30)
        try:
            with session_scope() as s:
                if get_state(s, LEASE_KEY).get("owner") == self.owner:
                    set_state(s, LEASE_KEY, {"owner": None, "expires": 0})
        except Exception:  # noqa: BLE001
            pass


def _wait_for_db(retries: int = 60) -> None:
    for i in range(retries):
        try:
            init_db()
            return
        except Exception as e:  # noqa: BLE001
            log.warning("database not ready (%s), retry %d/%d", e, i + 1, retries)
            time.sleep(2)
    raise SystemExit("database unavailable")


def main() -> None:
    setup_logging("worker")
    st = get_settings()
    log.info("starting worker (mode=%s, account=%s)", st.data_mode, st.account_name)
    _wait_for_db()
    w = Worker()

    def _sig(*_):
        w.stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    w.run()
    sys.exit(0)


def start_in_background() -> Worker:
    """Used when RUN_WORKER_IN_API=true (single-container hosting)."""
    w = Worker()
    threading.Thread(target=w.run, name="worker-main", daemon=True).start()
    return w


if __name__ == "__main__":
    main()
