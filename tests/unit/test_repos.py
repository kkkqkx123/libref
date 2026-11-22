"""Tests for repository file-walk skip rules."""

from __future__ import annotations

import pytest

from libref.astminer.repos import _should_skip


@pytest.mark.parametrize(
    "rel",
    [
        "test/test_mod.py",
        "testing/helper.py",
        "__tests__/suite.py",
        ".git/config",
        "node_modules/pkg.py",
        "src/gen/foo.pb2.py",
        "test_example.py",
        "example_test.py",
        "conftest.py",
        "test.py",
    ],
)
def test_skipped_paths(rel: str) -> None:
    assert _should_skip(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "latest/api.py",
        "contest/entry.py",
        "protest_case/lib.py",
        "tests/mod.py",
        "src/module.py",
        "atlas/main.go",
    ],
)
def test_kept_paths(rel: str) -> None:
    assert not _should_skip(rel)
