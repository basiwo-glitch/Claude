"""Container health check: `python -m app.healthcheck worker|api` (exit 0 = healthy)."""
from __future__ import annotations

import socket
import sys
import urllib.request

from app.db import get_state, init_db, now, session_scope


def worker() -> int:
    init_db()
    with session_scope() as s:
        hb = get_state(s, "worker_heartbeat")
    if not hb or now() - hb.get("ts", 0) > 90:
        print("no recent heartbeat")
        return 1
    if hb.get("host") != socket.gethostname():
        # heartbeat belongs to another instance; this one may be a standby
        print("heartbeat from other host", hb.get("host"))
        return 0
    return 0


def api() -> int:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5) as r:
            return 0 if r.status == 200 else 1
    except Exception as e:  # noqa: BLE001
        print(e)
        return 1


if __name__ == "__main__":
    sys.exit({"worker": worker, "api": api}[sys.argv[1] if len(sys.argv) > 1 else "worker"]())
