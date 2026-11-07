"""测试公共 fixture：临时库连接（已应用迁移）。"""

from __future__ import annotations

import sqlite3

import pytest

from libref.db import apply_migrations, open_db


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = open_db(tmp_path / "test.db")
    apply_migrations(c)
    yield c
    c.close()
