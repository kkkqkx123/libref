"""Client probe flags reach the server with the same names and meanings."""

from __future__ import annotations

import pytest
from librefctl.app import build_parser

pytestmark = pytest.mark.unit


def test_doc_probe_flags() -> None:
    args = build_parser().parse_args(
        [
            "doc-probe",
            "https://d.example/",
            "--sample-limit",
            "7",
            "--fetch-sample-limit",
            "3",
            "--max-pages",
            "10",
            "--prefilter",
        ]
    )
    assert args.start_url == "https://d.example/"
    assert args.sample_limit == 7
    assert args.fetch_sample_limit == 3
    assert args.max_pages == 10
    assert args.prefilter is True


def test_repo_probe_repeated_filters() -> None:
    args = build_parser().parse_args(
        [
            "repo-probe",
            "https://github.com/org/repo.git",
            "--only-file",
            "a.py",
            "--only-file",
            "b.py",
            "--exclude-pattern",
            r"(^|/)tests/",
            "--tree-depth",
            "3",
            "--samples-per-dir",
            "2",
        ]
    )
    assert args.source == "https://github.com/org/repo.git"
    assert args.only_files == ["a.py", "b.py"]
    assert args.exclude_patterns == [r"(^|/)tests/"]
    assert args.tree_depth == 3
    assert args.samples_per_dir == 2


def test_link_stats_flags() -> None:
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


def test_repo_probe_rejects_local_paths_for_http_exits(tmp_path) -> None:
    from libref.services.code.probe import probe_repo

    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "sample.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CLI-only"):
        probe_repo(str(folder), allow_local=False)

    assert probe_repo(str(folder)).root_kind == "local"
