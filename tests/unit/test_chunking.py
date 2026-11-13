"""Chunking tests: structural splitting, greedy merging, fence balance, dual-path alignment, overlap and budget."""

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


def test_heading_paths(monkeypatch, tmp_path):
    chunks = split_markdown(SAMPLE, "https://docs.example.com/guide")
    paths = {c.path for c in chunks}
    assert "Guide" in paths
    assert "Guide / Install" in paths
    assert "Guide / Install / Pip" in paths
    assert all(c.url == "https://docs.example.com/guide" for c in chunks)


def test_fence_hash_not_heading(monkeypatch, tmp_path):
    chunks = split_markdown(SAMPLE, "u")
    usage = [c for c in chunks if c.path == "Guide / Usage"]
    assert usage, "Usage section missing"
    assert any("this is a comment" in c.content for c in usage)
    assert not any(c.title == "this is a comment, not a heading" for c in chunks)


def test_greedy_merge_small_same_path_sections(monkeypatch, tmp_path):
    chunks = split_markdown("# A\nxx\n# A\nyy\n", "u")
    assert len(chunks) == 1
    assert chunks[0].merged_from == ["A", "A"]


def test_fence_balanced_invariant(monkeypatch, tmp_path):
    budgets = _budgets(embed={"hard_limit": 100, "min_limit": 10, "overlap": 5})
    long_md = "## Big\n\n" + "```python\nprint(1)\n```\n\n" + "word line. " * 300
    chunks = split_markdown(long_md, "u", budgets=budgets)
    assert len(chunks) > 1
    for c in chunks:
        fences = sum(
            1 for ln in c.content.splitlines() if ln.startswith(("```", "~~~"))
        )
        assert fences % 2 == 0, f"unbalanced fences in chunk {c.sort_order}"


def test_sort_order_sequential(monkeypatch, tmp_path):
    chunks = split_markdown(SAMPLE, "u")
    assert [c.sort_order for c in chunks] == list(range(len(chunks)))


def test_dual_streams_share_source_group(monkeypatch, tmp_path):
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


def test_overlap_is_previous_only_with_source(monkeypatch, tmp_path):
    body = "alpha beta gamma delta epsilon zeta eta theta.\n\n" * 12
    budgets = _budgets(bm25={"hard_limit": 40, "min_limit": 5, "overlap": 5})
    dual = split_markdown_dual(f"# Long\n\n{body}", "u", budgets=budgets)
    assert len(dual.bm25) > 1
    assert dual.bm25[0].overlap_from is None
    assert dual.bm25[1].overlap_from == dual.bm25[0].chunk_id


def test_span_kind_is_document_range(monkeypatch, tmp_path):
    for chunk in split_markdown(SAMPLE, "u"):
        assert chunk.span_kind == "document_range"


def test_twin_chunks_deduped_to_embedding(monkeypatch, tmp_path):
    dual = split_markdown_dual(SAMPLE, "u")
    assert dual.embedding
    assert dual.deduped >= 1
    assert dual.empty_dropped == 0


def test_token_estimate_separates_character_classes():
    """The estimate charges letters, symbols, whitespace and CJK differently."""
    from libref.chunking.budget import estimate_tokens

    letters = "abcdefghij" * 10
    symbols = "!" * 100
    cjk = "\u68c0\u7d22\u670d\u52a1\u4ece\u5168\u6587\u7d22\u5f15\u53ec\u56de" * 9
    assert (
        estimate_tokens(symbols) > estimate_tokens(letters) > estimate_tokens(" " * 100)
    )
    assert estimate_tokens(cjk) > estimate_tokens(letters)
    assert estimate_tokens(" \t\n  ") == 0


def test_token_estimate_tracks_measured_usage():
    """Calibration anchors: each sample's real token count was measured on the embedding model.

    The ratios bound how far the per-class weights may drift before the budget stops
    describing real token counts.
    """
    from libref.chunking.budget import estimate_tokens

    samples = {
        "english_prose": (
            "The quick brown fox jumps over the lazy dog while the retrieval service "
            "assembles candidate passages from the full text index and the vector store. "
            "Each candidate carries a section identifier and a short excerpt."
        )
        * 4,
        "chinese_prose": (
            "\u68c0\u7d22\u670d\u52a1\u4ece\u5168\u6587\u7d22\u5f15\u4e0e\u5411\u91cf\u5e93\u4e24\u8def\u53ec\u56de\u5019\u9009\u6bb5\u843d\uff0c"
            "\u518d\u6309\u878d\u5408\u5206\u503c\u6392\u5e8f\u5e76\u7ec4\u88c5\u6750\u6599\uff0c"
            "\u6bcf\u6761\u6750\u6599\u643a\u5e26\u5c0f\u8282\u6807\u8bc6\u4e0e\u7247\u6bb5\u6458\u5f55\uff0c"
            "\u8d85\u51fa\u9884\u7b97\u65f6\u6574\u6761\u4e22\u5f03\u5e76\u544a\u77e5\u6a21\u578b\u7701\u7565\u4e86\u591a\u5c11\u5355\u5143\u3002"
        )
        * 4,
        "python_code": (
            "def _collapse_duplicates(conn, fused):\n"
            "    '''Drop the auxiliary twin.'''\n"
            "    marks = ','.join('?' for _ in fused)\n"
            "    rows = conn.execute(f'SELECT id, url FROM documents WHERE id IN ({marks})', fused).fetchall()\n"
            "    return [row[0] for row in rows if row[1]]\n"
        )
        * 3,
        "markdown_symbols": (
            "```\n|---|:--:|---|\n| a | **b** | `c` |\n```\n"
            "- [x] done\n- [ ] todo\n# h1\n## h2\n"
        )
        * 4,
    }
    measured = {
        "english_prose": 214,
        "chinese_prose": 251,
        "python_code": 315,
        "markdown_symbols": 183,
    }
    for name, text in samples.items():
        ratio = estimate_tokens(text) / measured[name]
        assert 0.75 <= ratio <= 1.35, f"{name}: estimate ratio {ratio:.2f}"


def test_terms_count_cjk_beyond_one(monkeypatch, tmp_path):
    assert count_terms("hello world") == 2
    assert count_terms("get_or_init") == 1
    assert count_terms("媒体服务器") > 1
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("   ") == 0


def test_chunks_respect_body_limit(monkeypatch, tmp_path):
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


def test_chunk_relations_keywords_offsets(monkeypatch, tmp_path) -> None:
    from libref.chunking import split_markdown_dual

    dual = split_markdown_dual(
        "# A\n\nfirst body here\n\n# B\n\nsecond body here\n", "u"
    )
    assert dual.embedding, "embedding stream is empty"
    first = dual.embedding[0]
    assert first.content_hash, "chunk hash is empty"
    assert first.keywords, "keywords are empty"
    assert first.start_char >= 0 and first.end_char > first.start_char
    if len(dual.embedding) > 1:
        assert dual.embedding[0].next_chunk_id == dual.embedding[1].chunk_id
        assert dual.embedding[1].prev_chunk_id == dual.embedding[0].chunk_id
