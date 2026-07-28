"""
GAME LOG — rotating file logging for debugging a live session.
==============================================================

The GUI shows the last few lines of a carrier's ledger and the AI's recent
moves, which is enough to play but not enough to answer "why did my fleet
disappear overnight?". That question needs a durable record of what the
session actually did: every command and its outcome, every AI decision,
every clock anomaly, and any exception a background thread swallowed.

Pure stdlib: `logging` with `RotatingFileHandler`. Nothing here is imported
by the engine — logging is a property of a *session*, not of the simulation,
so a scenario or the explorer runs exactly as before with no log at all.

SIZING (the default is aimed at a 24-hour play session)
-------------------------------------------------------
Nothing here logs per tick. Volume comes from DECISIONS and EVENTS — human
commands, AI network/fleet reviews (days apart by construction), lease
expiries, clock anomalies — so it scales with how much happens, not with how
fast the clock runs.

MEASURED, not estimated: 2,000 sim-days of a three-AI-carrier data world
produced 16,411 bytes across 104 lines, i.e. ~8.2 KB per 1,000 sim-days. At
the default speed (0.5 sim-days/second) a 24-hour session covers ~43,200
sim-days, so about **0.4 MB a day**. Human commands add to that, but a player
issuing a command every ten seconds for a solid day adds only ~1 MB more.

The default cap is 4 MB x 6 files = 24 MB, roughly SIXTY days of continuous
play — so in practice rotation never fires during a session and the live file
holds the whole run, which is what you want to hand to a bug report. The cap
exists for the pathological cases (a session left running for weeks, a much
faster speed setting, a future DEBUG-level trace), where it guarantees the
newest 24 MB survives and the disk does not fill.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from typing import Optional

LOGGER_NAME = "airlinesim"

# See the sizing note above. Both are overridable per session (CLI: --log-max-mb
# / --log-backups), and the CLI defaults must stay in step with these.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024      # 4 MB per file
DEFAULT_BACKUPS = 5                       # + the live file => 24 MB worst case

_configured: Optional[str] = None


def default_log_path() -> str:
    """Where logs go when the caller doesn't say. Honors XDG-ish conventions."""
    base = (os.environ.get("AIRLINESIM_LOG_DIR")
            or os.path.join(os.path.expanduser("~"), ".airlinesim", "logs"))
    return os.path.join(base, "airlinesim.log")


def configure(path: Optional[str] = None, level: str = "INFO",
              max_bytes: int = DEFAULT_MAX_BYTES,
              backups: int = DEFAULT_BACKUPS,
              also_stderr: bool = False) -> Optional[str]:
    """
    Attach a rotating file handler to the package logger. Returns the path
    actually written to, or None if logging could not be set up (a read-only
    or missing directory must never take the game down with it).

    Calling twice with the same path is a no-op, so a server restart inside
    one process doesn't stack handlers and write every line N times.
    """
    global _configured
    path = path or default_log_path()
    if _configured == path:
        return path

    logger = logging.getLogger(LOGGER_NAME)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    except OSError as exc:                      # unwritable dir, bad path, ...
        logger.addHandler(logging.NullHandler())
        logger.warning("file logging disabled: %s", exc)
        return None

    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S"))
    # Replace rather than append, so reconfiguring to a new path doesn't keep
    # writing to the old one as well.
    for old in list(logger.handlers):
        logger.removeHandler(old)
        try:
            old.close()
        except Exception:
            pass
    logger.addHandler(handler)
    if also_stderr:
        logger.addHandler(logging.StreamHandler())
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    # Don't also spray the root logger's handlers (the CLI prints its own
    # output, and a duplicated game log in the terminal is just noise).
    logger.propagate = False
    _configured = path
    return path


def get(name: str = "") -> logging.Logger:
    """A child logger, e.g. get('session') -> 'airlinesim.session'."""
    logger = logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
    # A library must never print warnings about having no handler; a game run
    # with logging switched off should be silent, not noisy.
    if not logging.getLogger(LOGGER_NAME).handlers:
        logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())
    return logger


def describe() -> str:
    """Human-readable summary of where logs are going, for the CLI banner."""
    if not _configured:
        return "file logging: off"
    for h in logging.getLogger(LOGGER_NAME).handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            total = h.maxBytes * (h.backupCount + 1)
            return (f"log: {_configured} "
                    f"({h.maxBytes // (1024*1024)} MB x {h.backupCount + 1} files, "
                    f"{total // (1024*1024)} MB cap)")
    return f"log: {_configured}"
