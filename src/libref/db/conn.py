"""连接管理：建连接并设置 PRAGMA，SQLite 只存真源数据。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_db(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接并设置 PRAGMA，调用方负责随后执行迁移。

    跨线程复用连接时需显式关闭线程亲和检查，其余路径保持默认。
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
