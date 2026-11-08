"""增量缓存：仓库名、路径、指纹、配置版本联合主键。"""

from __future__ import annotations

import json
import sqlite3


def lookup(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    fp: str,
    cfg_version: int,
) -> tuple[str, list[dict]] | None:
    row = conn.execute(
        "SELECT file_sha1, records FROM ast_cache"
        " WHERE repo=? AND path=? AND fp=? AND cfg_version=?",
        (repo, path, fp, cfg_version),
    ).fetchone()
    if row is None:
        return None
    return row[0], [dict(r) for r in json.loads(row[1])]


def store(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    file_sha1: str,
    fp: str,
    cfg_version: int,
    records: list[dict],
) -> None:
    conn.execute(
        "INSERT INTO ast_cache(repo, path, file_sha1, fp, cfg_version, records)"
        " VALUES(?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, path, fp, cfg_version)"
        " DO UPDATE SET file_sha1=excluded.file_sha1, records=excluded.records",
        (repo, path, file_sha1, fp, cfg_version, json.dumps(records, ensure_ascii=False)),
    )
