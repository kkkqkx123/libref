"""分块测试：结构切分、贪心合并、围栏平衡、双路对齐、重叠与预算。"""

from __future__ import annotations

from libref.chunking import (
    ChunkBudget,
    ChunkPath,
    count_terms,
    estimate_tokens,
    split_markdown,
    split_markdown_dual,
)

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


def _budgets(embed=None, bm25=None):
    from libref.chunking import default_budgets

    budgets = default_budgets()
    for path, kwargs in ((ChunkPath.EMBEDDING, embed), (ChunkPath.BM25, bm25)):
        if kwargs is None:
            continue
        current = budgets[path]
        budgets[path] = ChunkBudget(
            hard_limit=kwargs.get("hard_limit", current.hard_limit),
            min_limit=kwargs.get("min_limit", current.min_limit),
            overlap=kwargs.get("overlap", current.overlap),
            reserve_ratio=kwargs.get("reserve_ratio", current.reserve_ratio),
        )
    return budgets


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


def test_greedy_merge_small_same_path_sections():
    chunks = split_markdown("# A\nxx\n# A\nyy\n", "u")
    assert len(chunks) == 1
    assert chunks[0].merged_from == ["A", "A"]


def test_fence_balanced_invariant():
    budgets = _budgets(embed={"hard_limit": 100, "min_limit": 10, "overlap": 5})
    long_md = "## Big\n\n" + "```python\nprint(1)\n```\n\n" + "word line. " * 300
    chunks = split_markdown(long_md, "u", budgets=budgets)
    assert len(chunks) > 1
    for c in chunks:
        fences = sum(
            1 for ln in c.content.splitlines() if ln.startswith(("```", "~~~"))
        )
        assert fences % 2 == 0, f"unbalanced fences in chunk {c.sort_order}"


def test_sort_order_sequential():
    chunks = split_markdown(SAMPLE, "u")
    assert [c.sort_order for c in chunks] == list(range(len(chunks)))


def test_dual_streams_share_source_group():
    body = "alpha beta gamma delta epsilon zeta eta theta.\n\n" * 12
    md_text = f"# Long\n\n{body}"
    budgets = _budgets(
        embed={"hard_limit": 512, "min_limit": 10, "overlap": 5},
        bm25={"hard_limit": 40, "min_limit": 5, "overlap": 2},
    )
    dual = split_markdown_dual(md_text, "u", budgets=budgets)
    assert len(dual.bm25) > 1
    assert len(dual.embedding) >= 1
    bm25_groups = {c.source_group_id for c in dual.bm25}
    embed_groups = {c.source_group_id for c in dual.embedding}
    assert bm25_groups & embed_groups
    assert all(c.chunk_id.startswith(f"{c.source_group_id}_") for c in dual.bm25)


def test_overlap_is_previous_only_with_source():
    body = "alpha beta gamma delta epsilon zeta eta theta.\n\n" * 12
    budgets = _budgets(bm25={"hard_limit": 40, "min_limit": 5, "overlap": 5})
    dual = split_markdown_dual(f"# Long\n\n{body}", "u", budgets=budgets)
    assert len(dual.bm25) > 1
    assert dual.bm25[0].overlap_from is None
    assert dual.bm25[1].overlap_from == dual.bm25[0].chunk_id
    assert dual.bm25[1].overlap_text.strip()


def test_span_kind_is_document_range():
    for chunk in split_markdown(SAMPLE, "u"):
        assert chunk.span_kind == "document_range"


def test_twin_chunks_deduped_to_embedding():
    dual = split_markdown_dual(SAMPLE, "u")
    assert dual.embedding
    assert dual.deduped >= 1
    assert dual.empty_dropped == 0


def test_terms_count_cjk_beyond_one():
    assert count_terms("hello world") == 2
    assert count_terms("get_or_init") == 1
    assert count_terms("媒体服务器") > 1
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("   ") == 0


def test_chunks_respect_body_limit():
    budgets = _budgets(
        embed={"hard_limit": 120, "min_limit": 10, "overlap": 5},
        bm25={"hard_limit": 30, "min_limit": 5, "overlap": 2},
    )
    long_md = "## Big\n\n" + "word line. " * 300
    dual = split_markdown_dual(long_md, "u", budgets=budgets)
    for chunk in dual.embedding:
        assert estimate_tokens(chunk.content) <= budgets[ChunkPath.EMBEDDING].body_limit
    for chunk in dual.bm25:
        assert count_terms(chunk.content) <= budgets[ChunkPath.BM25].body_limit
