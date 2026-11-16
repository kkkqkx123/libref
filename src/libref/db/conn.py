"""Connection management: create connections and set PRAGMAs; SQLite stores source-of-truth data only."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


def open_db(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection and set PRAGMAs; the caller is responsible for running migrations afterwards.

    When reusing a connection across threads the thread-affinity check must be explicitly
    disabled; all other paths keep the default.
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def write_lock(handle) -> threading.RLock:
    """Reentrant write guard shared by every writer on one storage handle.

    Metadata sessions carry their own guard and it is used directly. Writers
    hold it only around synchronous statement batches, never across awaits or
    sleeps, so the event loop never blocks on it.
    """
    own = getattr(handle, "write_lock", None)
    if callable(own) and not isinstance(handle, sqlite3.Connection):
        return own()
    guard = getattr(handle, "_libref_write_guard", None)
    if guard is None:
        guard = threading.RLock()
        try:
            handle._libref_write_guard = guard  # type: ignore[attr-defined]
        except AttributeError:
            return guard
    return guard
