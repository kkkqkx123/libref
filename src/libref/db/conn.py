"""连接管理：PRAGMA 设置。

向量存储/检索在 Qdrant（store/vec_store.py，见 docs/plan/qdrant-migration.md），
SQLite 保持纯 SQL 真源。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_db(path: str | Path) -> sqlite3.Connection:
    """打开连接并设置 PRAGMA；调用方负责随后 apply_migrations。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
