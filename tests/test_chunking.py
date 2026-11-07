"""分块测试：结构切分 / 贪心合并 / 围栏平衡不变量。"""

from __future__ import annotations

from libref.chunking import split_markdown

SAMPLE = """# Guide

intro paragraph.

## Install

run pip install libref.

### Pip

use pip.

### Conda

use conda.

## Usage

```python
# this is a comment, not a heading
print("hello")
```

more usage text.
"""


def test_heading_paths():
    chunks = split_markdown(SAMPLE, "https://docs.example.com/guide")
    paths = {c.path for c in chunks}
    assert "Guide" in paths
    assert "Guide / Install" in paths
    assert "Guide / Install / Pip" in paths
    assert all(c.url == "https://docs.example.com/guide" for c in chunks)


def test_fence_hash_not_heading():
    chunks = split_markdown(SAMPLE, "u")
    usage = [c for c in chunks if c.path == "Guide / Usage"]
    assert usage, "Usage section missing"
    assert any("this is a comment" in c.content for c in usage)
    assert not any(c.title == "this is a comment, not a heading" for c in chunks)


def test_greedy_merge_small_sections():
    # 两个极小的同路径小节应被合并，块数收敛
    chunks = split_markdown(SAMPLE, "u", min_chars=500)
    assert len(chunks) < 8


def test_fence_balanced_invariant():
    long_md = "## Big\n\n" + "```python\nprint(1)\n```\n\n" + "word line. " * 300
    chunks = split_markdown(long_md, "u", target_chars=200)
    for c in chunks:
        fences = sum(
            1 for ln in c.content.splitlines() if ln.startswith(("```", "~~~"))
        )
        assert fences % 2 == 0, f"unbalanced fences in chunk {c.sort_order}"


def test_sort_order_sequential():
    chunks = split_markdown(SAMPLE, "u")
    assert [c.sort_order for c in chunks] == list(range(len(chunks)))
