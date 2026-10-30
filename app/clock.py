"""Single source of time. Tests can freeze/advance it to simulate days in seconds."""
from __future__ import annotations

import time

_override: list[float] | None = None


def now() -> float:
    return _override[0] if _override is not None else time.time()


def set_clock(ref: list[float] | None) -> None:
    """Pass a one-element list to control time (ref[0] = epoch seconds); None = real time."""
    global _override
    _override = ref
