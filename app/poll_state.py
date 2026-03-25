"""Shared state for IMAP fetch + HARO processing so the Inbound page can show live progress."""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()

_state: dict[str, Any] = {
    "active": False,
    "phase": "idle",
    "message": "Idle — fetches run at startup, after you save a mailbox, and on a timer.",
    "started_at": None,
    "finished_at": None,
    "saved": None,
    "processed": None,
    "error": None,
}


def _now() -> float:
    return time.time()


def snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def poll_begin() -> None:
    with _lock:
        _state["active"] = True
        _state["phase"] = "starting"
        _state["message"] = "Starting…"
        _state["error"] = None
        _state["started_at"] = _now()
        _state["finished_at"] = None
        _state["saved"] = None
        _state["processed"] = None


def poll_fetching() -> None:
    with _lock:
        _state["phase"] = "fetching"
        _state["message"] = "Downloading messages from your mailbox (IMAP)…"


def poll_processing() -> None:
    with _lock:
        _state["phase"] = "processing"
        _state["message"] = "Parsing HARO digests and running classification…"


def poll_finish_ok(saved: int, processed: int) -> None:
    with _lock:
        _state["active"] = False
        _state["phase"] = "idle"
        _state["saved"] = saved
        _state["processed"] = processed
        _state["finished_at"] = _now()
        _state["message"] = "Finished. Saved %s new message(s), processed %s request(s)." % (saved, processed)
        _state["error"] = None


def poll_finish_err(err: str) -> None:
    with _lock:
        _state["active"] = False
        _state["phase"] = "error"
        _state["error"] = err
        _state["finished_at"] = _now()
        _state["message"] = err


def notify_skipped() -> None:
    """Another poll was already running (non-blocking lock lost)."""
    with _lock:
        _state["active"] = False
        _state["phase"] = "skipped"
        _state["message"] = "Skipped — another fetch was already running."
        _state["finished_at"] = _now()
