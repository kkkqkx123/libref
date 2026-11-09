"""版本保留治理：删除留痕与整版清理的唯一位置，不做自动后台清理。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


_DELETION_LOG_SQL = """
CREATE TABLE IF NOT EXISTS deletion_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library TEXT NOT NULL,
  version TEXT NOT NULL,
  ecosystem TEXT NOT NULL DEFAULT 'custom',
  reason TEXT NOT NULL,
  detail JSON NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_deletion_log(conn: sqlite3.Connection) -> None:
    conn.execute(_DELETION_LOG_SQL)
    conn.commit()


def log_deletion(
    conn: sqlite3.Connection,
    *,
    library: str,
    version: str,
    ecosystem: str = "custom",
    reason: str,
    detail: dict[str, Any] | None = None,
) -> int:
    ensure_deletion_log(conn)
    cur = conn.execute(
        "INSERT INTO deletion_log(library, version, ecosystem, reason, detail)"
        " VALUES(?,?,?,?,?)",
        (library, version, ecosystem, reason, json.dumps(detail or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_deletions(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    ensure_deletion_log(conn)
    rows = conn.execute(
        "SELECT id, library, version, ecosystem, reason, detail, created_at"
        " FROM deletion_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            detail = json.loads(row[5]) if row[5] else {}
        except ValueError:
            detail = {}
        out.append(
            {
                "id": row[0],
                "library": row[1],
                "version": row[2],
                "ecosystem": row[3],
                "reason": row[4],
                "detail": detail,
                "created_at": row[6],
            }
        )
    return out


def prune_expired_logs(conn: sqlite3.Connection, ttl_days: float) -> int:
    ensure_deletion_log(conn)
    cutoff = time.time() - max(0.0, float(ttl_days)) * 86400
    cur = conn.execute(
        "DELETE FROM deletion_log WHERE CAST(strftime('%s', created_at) AS REAL) < ?",
        (cutoff,),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def dir_size_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def check_quota(root: Path, quota_mb: int, label: str) -> None:
    quota_bytes = max(0, int(quota_mb)) * 1024 * 1024
    if quota_bytes <= 0:
        return
    if dir_size_bytes(root) > quota_bytes:
        raise ValueError(f"{label} over quota: {root} exceeds {quota_mb}MB, purge first")


def purge_version(
    conn: sqlite3.Connection,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    repos_dir: Path | None = None,
    snapshot_root: Path | None = None,
    vec_store: Any | None = None,
) -> dict[str, int]:
    from libref.store.snapshots import SnapshotStore

    ensure_deletion_log(conn)
    if ecosystem is not None:
        rows = conn.execute(
            "SELECT v.id, l.ecosystem FROM versions v"
            " JOIN libraries l ON l.id = v.library_id"
            " WHERE l.name=? AND l.ecosystem=? AND v.version=?",
            (library, ecosystem, version),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT v.id, l.ecosystem FROM versions v"
            " JOIN libraries l ON l.id = v.library_id"
            " WHERE l.name=? AND v.version=?",
            (library, version),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("library version must resolve to a single version")
    version_id = int(rows[0][0])
    eco = str(rows[0][1])
    doc_rows = conn.execute(
        "SELECT id FROM documents WHERE version_id=?", (version_id,)
    ).fetchall()
    removed_docs = len(doc_rows)
    removed_doc_ids = [int(r[0]) for r in doc_rows]
    if vec_store is not None:
        try:
            vec_store.delete_docs(removed_doc_ids)
        except Exception:
            pass
        try:
            vec_store.delete_version(version_id)
        except Exception:
            pass
    conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))
    dead_calls = conn.execute(
        "SELECT id FROM call_instances WHERE version_id=?", (version_id,)
    ).fetchall()
    for (dead_id,) in dead_calls:
        conn.execute(
            "DELETE FROM review_status WHERE entity_type='call_instance' AND entity_id=?",
            (int(dead_id),),
        )
    conn.execute("DELETE FROM call_instances WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM api_entities WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM pages WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM versions WHERE id=?", (version_id,))
    snapshot_removed = 0
    if snapshot_root is not None:
        snapshot_removed = SnapshotStore(snapshot_root).drop_version(eco, library, version)
    repos_removed = 0
    if repos_dir is not None:
        repo_name = f"{eco}/{library}/{version}"
        row = conn.execute("SELECT local_path FROM repos WHERE name=?", (repo_name,)).fetchone()
        conn.execute("DELETE FROM repos WHERE name=?", (repo_name,))
        if row is not None and row[0]:
            import shutil

            try:
                candidate = Path(row[0]).resolve()
                base = Path(repos_dir).resolve()
                if candidate != base and base in candidate.parents:
                    shutil.rmtree(candidate, ignore_errors=True)
                    repos_removed = 1
            except Exception:
                repos_removed = 0
    conn.commit()
    log_deletion(
        conn,
        library=library,
        version=version,
        ecosystem=eco,
        reason="purge",
        detail={"removed_docs": removed_docs, "snapshots": snapshot_removed},
    )
    return {
        "removed_docs": removed_docs,
        "snapshot_files": snapshot_removed,
        "repos_removed": repos_removed,
    }
