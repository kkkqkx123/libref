"""Probe exit passthrough: CLI flags reach services with the same names and meanings."""

from __future__ import annotations

import pytest

from libref.cli.common import _scope_from_args
from libref.cli.parsers import build_parser

pytestmark = pytest.mark.unit


def test_probe_docs_cli_flags() -> None:
    args = build_parser().parse_args(
        [
            "probe-docs",
            "https://d.example/",
            "--max-pages",
            "10",
            "--prefilter",
            "--follow-links",
            "--crawl-query",
            "guide",
            "--score-threshold",
            "0.5",
            "--sample-limit",
            "7",
        ]
    )
    assert args.max_pages == 10
    assert args.prefilter is True
    assert args.sample_limit == 7
    scope = _scope_from_args(args)
    assert scope["follow_links"] is True
    assert scope["crawl_query"] == "guide"
    assert scope["score_threshold"] == 0.5


def test_probe_repo_cli_only_file() -> None:
    args = build_parser().parse_args(
        ["probe-repo", "/tmp/x", "--only-file", "a.py", "--only-file", "b.py"]
    )
    assert args.only_file == ["a.py", "b.py"]
    assert args.subdir is None
