"""Unit tests for the path-list census shared by local walks and remote tiers."""

import pytest

from libref.astminer.repos import census_of_relpaths

pytestmark = pytest.mark.unit


def test_filters_suffix_skip_and_oversize() -> None:
    items = [
        ("guide/a.py", 10),
        ("guide/test_b.py", 10),
        ("docs/g.md", 10),
        ("big.py", 2_000_000),
        ("a.pyc", 10),
    ]
    census = census_of_relpaths(items, suffixes=(".py", ".md"), subdir=None)
    assert census["files"] == ["docs/g.md", "guide/a.py"]
    assert census["skipped"] == 1
    assert census["oversized"] == 1
    assert census["suffix_counts"] == {".py": 3, ".md": 1}
    assert census["top_dirs"] == {"guide": 2, "docs": 1, "(root)": 1}


def test_subdir_and_unknown_size() -> None:
    items = [("pkg/a.py", None), ("pkg/sub/b.py", 10), ("other/c.py", 10)]
    census = census_of_relpaths(items, suffixes=(".py",), subdir="pkg")
    assert census["files"] == ["pkg/a.py", "pkg/sub/b.py"]
    assert census["oversized"] == 0


def test_stat_failure_counts_as_skipped() -> None:
    census = census_of_relpaths([("a.py", -1)], suffixes=(".py",))
    assert census["files"] == []
    assert census["skipped"] == 1


def test_exclude_patterns_drop_whole_subtrees() -> None:
    items = [
        ("pkg/a.py", 10),
        ("pkg/tests/t.py", 10),
        ("vendor/v.py", 10),
        ("docs/g.md", 10),
    ]
    census = census_of_relpaths(
        items, suffixes=(".py", ".md"), exclude_patterns=[r"(^|/)tests/", r"^vendor/"]
    )
    assert census["files"] == ["docs/g.md", "pkg/a.py"]
    assert census["suffix_counts"] == {".py": 1, ".md": 1}
    assert census["top_dirs"] == {"pkg": 1, "docs": 1}
    with pytest.raises(ValueError, match="invalid exclude pattern"):
        census_of_relpaths(items, suffixes=(".py",), exclude_patterns=["("])


def test_dir_files_counts_recursively() -> None:
    items = [
        ("pkg/a.py", 10),
        ("pkg/sub/b.py", 10),
        ("pkg/sub/deep/c.py", 10),
        ("other/d.py", 10),
    ]
    census = census_of_relpaths(items, suffixes=(".py",))
    assert census["dir_files"] == {
        "pkg": 3,
        "pkg/sub": 2,
        "pkg/sub/deep": 1,
        "other": 1,
    }
