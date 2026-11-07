"""连接管理：PRAGMA 设置。

向量存储/检索在 Qdrant（store/vec_store.py，见 docs/plan/qdrant-migration.md），
SQLite 保持纯 SQL 真源。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_db(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并设置 PRAGMA；调用方负责随后 apply_migrations。

    stdio 服务（mcp_server/server 经 MCPServer 在 worker 线程执行工具）
    需显式 check_same_thread=False；其余路径保持默认线程亲和。
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
