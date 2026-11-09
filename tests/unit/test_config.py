"""config 测试：默认值 + 环境变量覆盖 + 文件加载优先级。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from libref.config import (
    ConfigError,
    LibrefSettings,
    load_settings,
    redacted_dict,
    resolve_config_file,
    settings_fingerprint,
)


def test_defaults(tmp_path, monkeypatch) -> None:
    # 家目录隔离下验证纯默认值：三路径均为绝对路径且相互独立
    _isolated_env(monkeypatch, tmp_path)
    s = LibrefSettings()
    assert s.db_path == tmp_path / ".local/share/libref/libref.db"
    assert s.repos_dir == tmp_path / ".local/share/libref/repos"
    assert s.snapshot_dir == tmp_path / ".local/share/libref/snapshots"
    assert s.fetch_concurrency == 4
    assert s.embedding_model == "BAAI/bge-m3"
    assert s.embedding_dim == 1024
    assert s.embedding_api_base == "https://api.siliconflow.cn/v1"
    assert s.embedding_api_key is None
    assert s.registry_bases["pypi"] == "https://pypi.org/pypi"
    assert s.registry_bases["npm"] == "https://registry.npmjs.org"
    assert s.registry_bases["maven"] == "https://repo1.maven.org/maven2"
    assert s.registry_mirror == "default"
    assert s.http_proxy is None
    assert s.no_proxy is None
    assert s.registry_overrides_file is None


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


def test_env_override(tmp_path, monkeypatch) -> None:
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBREF_DB_PATH", "/tmp/other.db")
    monkeypatch.setenv("LIBREF_FETCH_CONCURRENCY", "8")
    s = LibrefSettings()
    assert s.db_path == Path("/tmp/other.db")
    assert s.fetch_concurrency == 8
    assert s.repos_dir == tmp_path / ".local/share/libref/repos"
    assert s.snapshot_dir == tmp_path / ".local/share/libref/snapshots"


def _isolated_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in [n for n in os.environ if n.startswith("LIBREF_")]:
        monkeypatch.delenv(name, raising=False)


def test_file_sections_and_top_level(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "libref.toml"
    conf.write_text(
        "fetch_concurrency = 7\n"
        "[crawler]\nrequest_timeout = 9.5\n"
        "[mcp]\nmcp_port = 9001\n",
        encoding="utf-8",
    )
    s = load_settings(conf)
    assert s.fetch_concurrency == 7
    assert s.request_timeout == 9.5
    assert s.mcp_port == 9001
    assert s.fetch_max_retries == 3


def test_priority_overrides_beat_env_beats_file(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "libref.toml"
    conf.write_text("[crawler]\nfetch_concurrency = 2\n", encoding="utf-8")
    assert load_settings(conf).fetch_concurrency == 2
    monkeypatch.setenv("LIBREF_FETCH_CONCURRENCY", "8")
    assert load_settings(conf).fetch_concurrency == 8
    assert load_settings(conf, {"fetch_concurrency": 1}).fetch_concurrency == 1


def test_dotenv_beats_file(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("LIBREF_FETCH_CONCURRENCY=6\n", encoding="utf-8")
    (tmp_path / "libref.toml").write_text(
        "[crawler]\nfetch_concurrency = 2\n", encoding="utf-8"
    )
    assert load_settings().fetch_concurrency == 6


def test_search_order_explicit_beats_cwd(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    (tmp_path / "libref.toml").write_text(
        "[crawler]\nfetch_concurrency = 2\n", encoding="utf-8"
    )
    other = tmp_path / "other.toml"
    other.write_text("[crawler]\nfetch_concurrency = 5\n", encoding="utf-8")
    assert load_settings().fetch_concurrency == 2
    assert load_settings(other).fetch_concurrency == 5
    assert resolve_config_file(other) == other


def test_missing_explicit_file(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "absent.toml")


def test_invalid_value_reports_source(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "libref.toml"
    conf.write_text("[mcp]\nmcp_port = 99999\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="libref.toml"):
        load_settings(conf)


def test_secret_in_file_rejected(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "libref.toml"
    conf.write_text(
        '[embeddings]\nembedding_api_key = "sk-test"\n', encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="embedding_api_key"):
        load_settings(conf)


def test_unknown_section_and_key_rejected(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    bad_section = tmp_path / "a.toml"
    bad_section.write_text("[nope]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nope"):
        load_settings(bad_section)
    bad_key = tmp_path / "b.toml"
    bad_key.write_text("[crawler]\nnope = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nope"):
        load_settings(bad_key)


def test_reserved_sections_now_enabled(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    filled = tmp_path / "a.toml"
    filled.write_text('[mcp.auth]\nmcp_require_auth = true\n', encoding="utf-8")
    assert load_settings(filled).mcp_require_auth is True
    web_conf = tmp_path / "b.toml"
    web_conf.write_text("[web]\nweb_port = 8100\n", encoding="utf-8")
    assert load_settings(web_conf).web_port == 8100
    empty = tmp_path / "c.toml"
    empty.write_text("[web]\n", encoding="utf-8")
    assert load_settings(empty).fetch_concurrency == 4


def test_mcp_token_in_file_rejected(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    conf = tmp_path / "a.toml"
    conf.write_text('[mcp.auth]\nmcp_token = "sk-test"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="mcp_token"):
        load_settings(conf)


def test_redaction_and_fingerprint(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBREF_EMBEDDING_API_KEY", "sk-test")
    monkeypatch.setenv("LIBREF_MCP_TOKEN", "tok-test")
    s = load_settings()
    dumped = redacted_dict(s)
    assert dumped["embedding_api_key"] == "***"
    assert dumped["mcp_token"] == "***"
    assert dumped["chat_api_key"] is None
    assert settings_fingerprint(s) == settings_fingerprint(load_settings())
    other = load_settings(None, {"fetch_concurrency": 9})
    assert settings_fingerprint(other) != settings_fingerprint(s)


def test_relative_paths_resolve_to_file_dir(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    sub = tmp_path / "env"
    sub.mkdir()
    conf = sub / "conf.toml"
    conf.write_text('[server]\ndb_path = "data/x.db"\n', encoding="utf-8")
    s = load_settings(conf)
    assert s.db_path == sub / "data" / "x.db"
    abs_conf = tmp_path / "abs.toml"
    abs_conf.write_text('[server]\ndb_path = "/tmp/x.db"\n', encoding="utf-8")
    assert load_settings(abs_conf).db_path == Path("/tmp/x.db")


def test_paths_absolute_and_independent(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    monkeypatch.setenv("LIBREF_DB_PATH", "relative/other.db")
    s = LibrefSettings()
    assert s.db_path == tmp_path / "relative/other.db"
    assert s.repos_dir.is_absolute()
    assert s.snapshot_dir.is_absolute()
    assert s.repos_dir != s.db_path.parent / "repos"
    assert s.snapshot_dir != s.db_path.parent / "snapshots"


def test_snapshot_is_frozen(monkeypatch, tmp_path) -> None:
    _isolated_env(monkeypatch, tmp_path)
    s = load_settings()
    with pytest.raises(ValidationError):
        s.fetch_concurrency = 9  # type: ignore[misc]
