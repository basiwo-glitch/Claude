"""Logging: human readable to stdout + rotating file with full tracebacks."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import get_settings

_configured = False


def setup_logging(process_name: str = "app") -> None:
    global _configured
    if _configured:
        return
    st = get_settings()
    level = getattr(logging, st.log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(process_name)s] %(name)s: %(message)s")

    class _Ctx(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.process_name = process_name
            return True

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh.addFilter(_Ctx())
    root.addHandler(sh)

    try:
        os.makedirs(st.log_dir, exist_ok=True)
        fh = RotatingFileHandler(
            os.path.join(st.log_dir, f"{process_name}.log"), maxBytes=10_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(_Ctx())
        root.addHandler(fh)
        eh = RotatingFileHandler(
            os.path.join(st.log_dir, f"{process_name}.errors.log"), maxBytes=10_000_000, backupCount=5, encoding="utf-8"
        )
        eh.setLevel(logging.WARNING)
        eh.setFormatter(fmt)
        eh.addFilter(_Ctx())
        root.addHandler(eh)
    except OSError:  # read-only FS -> stdout only
        root.warning("file logging disabled (log dir not writable)")

    for noisy in ("urllib3", "sqlalchemy.engine", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
