"""Scope tests: default behavior unchanged, suffix governance, relevance scoring."""

from __future__ import annotations

from libref.crawler.scope import (
    build_scope,
    content_type_allowed,
    score_url,
    sort_by_relevance,
)

START = "https://docs.example.com/guide/"


def test_defaults_reject_binaries_and_keep_docs(monkeypatch, tmp_path) -> None:
    policy = build_scope(START, None)
    assert policy.accept("https://docs.example.com/guide/install.html")
    assert not policy.accept("https://docs.example.com/logo.png")
    assert not policy.accept("https://docs.example.com/manual.pdf")
    assert not policy.accept("https://other.example.com/x.html")


def test_suffix_blocklist_overridable(monkeypatch, tmp_path) -> None:
    policy = build_scope(START, {"excluded_suffixes": []})
    assert policy.accept("https://docs.example.com/logo.png")
    policy = build_scope(START, {"allowed_suffixes": [".png"]})
    assert policy.accept("https://docs.example.com/logo.png")
    assert not policy.accept("https://docs.example.com/guide/a.html")


def test_content_type_helper(monkeypatch, tmp_path) -> None:
    assert content_type_allowed("https://h/a.html", "text/html")
    assert not content_type_allowed("https://h/a.png", None)
    assert not content_type_allowed("https://h/a", "image/png")
    assert content_type_allowed("https://h/a", None)


def test_relevance_scoring_and_sort(monkeypatch, tmp_path) -> None:
    urls = [
        "https://docs.example.com/about/team.html",
        "https://docs.example.com/install/pip.html",
        "https://docs.example.com/install/source.html",
    ]
    assert sort_by_relevance(urls, None) == urls
    ranked = sort_by_relevance(urls, "pip install")
    assert ranked[0] == "https://docs.example.com/install/pip.html"
    assert score_url("https://docs.example.com/install/pip.html", "pip") > score_url(
        "https://docs.example.com/about/team.html", "pip"
    )


def test_filter_stats_counts(monkeypatch, tmp_path) -> None:
    policy = build_scope(START, None)
    accepted, rejected, stats = policy.filter_with_stats(
        [
            "https://docs.example.com/guide/a.html",
            "https://docs.example.com/x.png",
            "https://other.example.com/y.html",
        ]
    )
    assert accepted == ["https://docs.example.com/guide/a.html"]
    assert len(rejected) == 2
    assert stats.accepted == 1 and stats.rejected == 2
    assert stats.content_type_rejected == 1
