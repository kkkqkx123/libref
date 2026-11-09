"""CJK run-expansion shared-leaf unit tests: original string kept, jieba tokens, cross-word bigrams, symmetric on both sides."""

from __future__ import annotations

from libref.text.bm25 import clean_text_for_bm25
from libref.text.cjk import expand_cjk_run
from libref.text.query import safe_fts_query


def test_expand_keeps_original_first():
    assert expand_cjk_run("媒体服务器")[0] == "媒体服务器"


def test_expand_covers_jieba_words_and_cross_boundary_bigrams():
    expanded = expand_cjk_run("媒体服务器")
    assert "服务器" in expanded
    assert "服务" in expanded
    assert "体服" in expanded


def test_expand_single_char_and_empty():
    assert expand_cjk_run("服") == ["服"]
    assert expand_cjk_run("") == []


def test_query_expands_substring_terms():
    query = safe_fts_query("服务器")
    assert '"服务器"' in query
    assert '"服务"' in query
    assert '"务器"' in query


def test_cleaning_and_query_share_vocabulary():
    cleaned = clean_text_for_bm25("媒体服务器")
    for term in expand_cjk_run("服务器"):
        assert term in cleaned
