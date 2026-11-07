"""config 测试：默认值 + 环境变量覆盖。"""

from __future__ import annotations

from pathlib import Path

from libref.config import LibrefSettings


def test_defaults(tmp_path, monkeypatch) -> None:
    # 在临时 cwd 跑，隔离仓库根 .env 的 dotenv 注入，验证纯默认值
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings()
    assert s.db_path == Path("data/libref.db")
    assert s.fetch_concurrency == 4
    assert s.embedding_model == "BAAI/bge-m3"
    assert s.embedding_dim == 1024
    assert s.embedding_api_base == "https://api.siliconflow.cn/v1"
    assert s.embedding_api_key is None


def test_dotenv_file(tmp_path, monkeypatch) -> None:
    # .env 经 env_file 生效（LIBREF_ 前缀），且不入库的 key 由该通道注入
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "LIBREF_EMBEDDING_MODEL=some/local-model\nLIBREF_EMBEDDING_API_KEY=sk-test\n",
        encoding="utf-8",
    )
    s = LibrefSettings()
    assert s.embedding_model == "some/local-model"
    assert s.embedding_api_key == "sk-test"


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("LIBREF_DB_PATH", "/tmp/other.db")
    monkeypatch.setenv("LIBREF_FETCH_CONCURRENCY", "8")
    s = LibrefSettings()
    assert s.db_path == Path("/tmp/other.db")
    assert s.fetch_concurrency == 8
