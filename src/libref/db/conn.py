"""Connection management: create connections and set PRAGMAs; SQLite stores source-of-truth data only."""

from __future__ import annotations

import sqlite3
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
    return conn
