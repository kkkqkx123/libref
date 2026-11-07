"""config 测试：默认值 + 环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

from libref.config import LibrefSettings


def test_defaults() -> None:
    s = LibrefSettings()
    assert s.db_path == Path("data/libref.db")
    assert s.fetch_concurrency == 4
    assert s.embedding_model == "BAAI/bge-small-zh"
    assert s.embedding_dim == 512


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("LIBREF_DB_PATH", "/tmp/other.db")
    monkeypatch.setenv("LIBREF_FETCH_CONCURRENCY", "8")
    s = LibrefSettings()
    assert s.db_path == Path("/tmp/other.db")
    assert s.fetch_concurrency == 8
