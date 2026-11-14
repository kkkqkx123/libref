"""Version-scoped ingest cursors: ordered-list offset plus fingerprint for resume.

One row per version and kind (docs or code). The fingerprint covers the
ordered target list; a mismatch means the discovery result changed shape and
the run restarts from zero instead of skipping the wrong segment.
"""

from __future__ import annotations

import hashlib
import sqlite3

from libref.db.schema import PROGRESS_SQL

DOCS_KIND = "docs"
CODE_KIND = "code"


def ensure_progress_table(conn: sqlite3.Connection) -> None:
    conn.executescript(PROGRESS_SQL)
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(ingest_progress)").fetchall()
    }
    if "vector_done" not in cols:
        conn.execute(
            "ALTER TABLE ingest_progress ADD COLUMN vector_done INTEGER NOT NULL DEFAULT 0"
        )
    if "pending" not in cols:
        conn.execute(
            "ALTER TABLE ingest_progress ADD COLUMN pending TEXT NOT NULL DEFAULT '[]'"
        )
    conn.commit()


def fingerprint_of(items: list[str]) -> str:
    digest = hashlib.sha1()
    for item in items:
        digest.update(item.encode("utf-8", "ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def load_progress(
    conn: sqlite3.Connection, version_id: int, kind: str
) -> dict[str, int | str] | None:
    ensure_progress_table(conn)
    row = conn.execute(
        "SELECT total, done, fingerprint, batch_size, vector_done FROM ingest_progress"
        " WHERE version_id=? AND kind=?",
        (version_id, kind),
    ).fetchone()
    if row is None:
        return None
    return {
        "total": int(row[0]),
        "done": int(row[1]),
        "fingerprint": str(row[2]),
        "batch_size": int(row[3]),
        "vector_done": int(row[4]),
    }


def save_progress(
    conn: sqlite3.Connection,
    version_id: int,
    kind: str,
    *,
    total: int,
    done: int,
    fingerprint: str,
    batch_size: int,
    vector_done: int | None = None,
) -> None:
    """Persist the resume cursor; an absent watermark implies the cursor."""
    from libref.db.conn import write_lock

    watermark = done if vector_done is None else vector_done
    ensure_progress_table(conn)
    with write_lock(conn):
        conn.execute(
            "INSERT INTO ingest_progress(version_id, kind, total, done,"
            " fingerprint, batch_size, vector_done) VALUES(?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(version_id, kind) DO UPDATE SET total=excluded.total,"
            " done=excluded.done, fingerprint=excluded.fingerprint,"
            " batch_size=excluded.batch_size, vector_done=excluded.vector_done,"
            " updated_at=datetime('now')",
            (version_id, kind, total, done, fingerprint, batch_size, watermark),
        )
        conn.commit()


def save_vector_watermark(
    conn: sqlite3.Connection, version_id: int, kind: str, vector_done: int
) -> None:
    """Record vector progress ahead of the cursor; the cursor follows afterwards."""
    from libref.db.conn import write_lock

    ensure_progress_table(conn)
    with write_lock(conn):
        conn.execute(
            "INSERT INTO ingest_progress(version_id, kind, vector_done)"
            " VALUES(?, ?, ?)"
            " ON CONFLICT(version_id, kind) DO UPDATE"
            " SET vector_done=excluded.vector_done, updated_at=datetime('now')",
            (version_id, kind, vector_done),
        )
        conn.commit()


def clear_progress(conn: sqlite3.Connection, version_id: int, kind: str) -> None:
    from libref.db.conn import write_lock

    ensure_progress_table(conn)
    with write_lock(conn):
        conn.execute(
            "DELETE FROM ingest_progress WHERE version_id=? AND kind=?",
            (version_id, kind),
        )
        conn.commit()


def save_pending(
    conn: sqlite3.Connection, version_id: int, kind: str, pending: list[str]
) -> None:
    """Persist the follow queue alongside the cursor; an empty queue clears the key."""
    import json as _json

    from libref.db.conn import write_lock

    ensure_progress_table(conn)
    with write_lock(conn):
        conn.execute(
            "INSERT INTO ingest_progress(version_id, kind, pending)"
            " VALUES(?, ?, ?)"
            " ON CONFLICT(version_id, kind) DO UPDATE"
            " SET pending=excluded.pending, updated_at=datetime('now')",
            (version_id, kind, _json.dumps(list(pending))),
        )
        conn.commit()


def load_pending(conn: sqlite3.Connection, version_id: int, kind: str) -> list[str]:
    """Follow queue saved by the previous attempt; empty when nothing was saved."""
    import json as _json

    ensure_progress_table(conn)
    row = conn.execute(
        "SELECT pending FROM ingest_progress WHERE version_id=? AND kind=?",
        (version_id, kind),
    ).fetchone()
    if row is None:
        return []
    try:
        items = _json.loads(str(row[0] or "[]"))
    except ValueError:
        return []
    return [str(u) for u in items] if isinstance(items, list) else []


def resume_offset(
    conn: sqlite3.Connection,
    version_id: int,
    kind: str,
    items: list[str],
    *,
    fresh: bool = False,
) -> tuple[int, str]:
    """Resume offset for an ordered target list; mismatch or fresh restarts at zero."""
    fingerprint = fingerprint_of(items)
    if fresh:
        clear_progress(conn, version_id, kind)
        return 0, fingerprint
    saved = load_progress(conn, version_id, kind)
    if saved is None:
        return 0, fingerprint
    if saved["fingerprint"] != fingerprint or int(saved["total"]) != len(items):
        clear_progress(conn, version_id, kind)
        return 0, fingerprint
    done = max(0, min(int(saved["done"]), len(items)))
    return done, fingerprint
