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


def test_probe_repo_exclude_and_tree_depth_cli() -> None:
    args = build_parser().parse_args(
        [
            "probe-repo",
            "/tmp/x",
            "--exclude-pattern",
            r"(^|/)tests/",
            "--tree-depth",
            "3",
        ]
    )
    assert args.exclude_pattern == [r"(^|/)tests/"]
    assert args.tree_depth == 3
    sampled = build_parser().parse_args(
        ["probe-repo", "/tmp/x", "--samples-per-dir", "2"]
    )
    assert sampled.samples_per_dir == 2
    indexed = build_parser().parse_args(
        ["index-repo", "demo", "0.1", "/tmp/x", "--exclude-pattern", "^vendor/"]
    )
    assert indexed.exclude_pattern == ["^vendor/"]


def test_link_stats_cli() -> None:
    args = build_parser().parse_args(["link-stats", "docs", "1.0", "--limit", "5"])
    assert (args.library, args.version, args.limit, args.url) == (
        "docs",
        "1.0",
        5,
        None,
    )
    singled = build_parser().parse_args(
        ["link-stats", "docs", "1.0", "--url", "https://d.example/a"]
    )
    assert singled.url == "https://d.example/a"
