"""Extract tests: composite pruning goldens, structured metadata/links, citations."""

from __future__ import annotations

from libref.crawler.extract import (
    convert_links_to_citations,
    extract_to_markdown,
    is_data_table,
    scrape_html,
)

NAV_PAGE = """<html><head><title>Guide</title>
<meta name="description" content="Install guide">
<link rel="canonical" href="/guide/">
</head><body>
<nav><ul><li><a href="/home">Home</a></li><li><a href="/login">Login</a></li></ul></nav>
<main><article><h1>Install</h1>
<p>Run the installer with the documented flags and wait for completion.</p>
<p>See the <a href="/guide/config">config reference</a> and
<a href="https://external.example/x">external notes</a>.</p>
<table><thead><tr><th>Flag</th><th>Meaning</th></tr></thead>
<tbody><tr><td>--fast</td><td>skip checks</td></tr>
<tr><td>--full</td><td>all components</td></tr></tbody></table>
</article></main>
<footer>copyright 2026</footer>
<script>var x = 1;</script>
</body></html>"""


def test_pruning_keeps_article_drops_nav_footer(monkeypatch, tmp_path) -> None:
    md = extract_to_markdown(NAV_PAGE, "https://docs.example.com/guide/")
    assert "Run the installer" in md
    assert "Home" not in md
    assert "copyright" not in md
    assert "var x" not in md


def test_structured_links_and_metadata(monkeypatch, tmp_path) -> None:
    res = scrape_html(NAV_PAGE, "https://docs.example.com/guide/")
    assert res.title == "Guide"
    assert res.description == "Install guide"
    assert res.canonical == "https://docs.example.com/guide/"
    assert not res.noindex and not res.nofollow
    assert "https://docs.example.com/guide/config" in res.internal_links
    assert "https://external.example/x" in res.external_links
    assert "Flag" in res.markdown and "--fast" in res.markdown


def test_noindex_page_reports_flag(monkeypatch, tmp_path) -> None:
    res = scrape_html(
        '<html><head><meta name="robots" content="noindex, nofollow"></head>'
        "<body><main><p>body text here and more words to pass</p></main></body></html>",
        "https://d.example/a.html",
    )
    assert res.noindex and res.nofollow


def test_data_table_detection(monkeypatch, tmp_path) -> None:
    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser("<table><tr><td>a</td></tr></table>")
    assert not is_data_table(tree.css_first("table"))
    tree = LexborHTMLParser(
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
    )
    assert is_data_table(tree.css_first("table"))


def test_citations_format(monkeypatch, tmp_path) -> None:
    converted, refs = convert_links_to_citations(
        "See [docs](https://d.example/a) and [docs](https://d.example/a).",
        "https://d.example/",
    )
    assert "⟨1⟩" in converted
    assert "## References" in refs
    assert "https://d.example/a" in refs


def test_malformed_html_yields_empty(monkeypatch, tmp_path) -> None:
    assert scrape_html("", None).markdown == ""
    assert extract_to_markdown("", None) == ""
