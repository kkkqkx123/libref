"""CLI exit boundary: failures flow through the shared error value and mapper."""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from libref.cli.common import as_error, report_failure
from libref.errors import (
    LibrefError,
    exit_code_for,
    invalid_argument,
    unavailable,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_argument", 2),
        ("config_refresh_failed", 2),
        ("unavailable", 1),
        ("not_found", 1),
    ],
)
def test_failure_codes_map_to_the_three_tiers(code: str, expected: int) -> None:
    assert exit_code_for(code) == expected


def test_report_failure_renders_text_and_json(capsys) -> None:
    error = invalid_argument("limit must be positive", field="limit")
    code = report_failure(error, argparse.Namespace(json=True))
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err.startswith("invalid_argument:")
    payload = json.loads(captured.out.strip())
    assert payload == {
        "error": "invalid_argument",
        "message": "limit must be positive",
        "field": "limit",
    }


def test_unknown_exception_is_not_swallowed() -> None:
    with pytest.raises(RuntimeError):
        as_error(RuntimeError("boom"))


def test_index_repo_missing_version_raises_invalid_argument(monkeypatch) -> None:
    from libref.cli import code_commands

    monkeypatch.setattr(
        code_commands,
        "_load_settings",
        lambda *a, **k: SimpleNamespace(repos_dir="/tmp/libref-repos"),
    )
    monkeypatch.setattr(
        code_commands, "_connect", lambda *a, **k: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(code_commands, "_build_vector", lambda *a, **k: (None, None))
    args = argparse.Namespace(source="/tmp/src", version=None, ecosystem=None)

    with pytest.raises(LibrefError) as raised:
        code_commands.cmd_index_repo(args)

    assert raised.value.code == "invalid_argument"
    assert exit_code_for(raised.value.code) == 2


def test_dump_chunks_unsupported_suffix_raises_invalid_argument(
    monkeypatch, tmp_path
) -> None:
    from libref.astminer import config as ast_config
    from libref.cli import docs_commands

    source = tmp_path / "sample.zzzunknown"
    source.write_bytes(b"x")
    settings = SimpleNamespace(
        chunk_reserve_ratio=0.1,
        chunk_embed_max_tokens=512,
        chunk_embed_min_tokens=64,
        chunk_embed_overlap_tokens=32,
        chunk_bm25_max_terms=256,
        chunk_bm25_min_terms=16,
        chunk_bm25_overlap_terms=8,
    )
    monkeypatch.setattr(docs_commands, "_load_settings", lambda *a, **k: settings)
    monkeypatch.setattr(ast_config, "config_for_suffix", lambda suffix: None)

    args = argparse.Namespace(markdown=None, source=str(source))

    with pytest.raises(LibrefError) as raised:
        docs_commands.cmd_dump_chunks(args)

    assert raised.value.code == "invalid_argument"
    assert raised.value.field == "source"


def test_unavailable_factory_keeps_outage_on_tier_one() -> None:
    error = unavailable("remote poll timeout")
    assert error.code == "unavailable"
    assert exit_code_for(error.code) == 1
