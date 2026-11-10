"""Discovery parsing and ranking tests: pure in-memory, no DB or network."""

from __future__ import annotations

from libref.crawler.github import (
    github_search_headers,
    github_search_url,
    parse_github_search,
)
from libref.crawler.registry.maven import (
    maven_search_url,
    parse_maven_search,
)
from libref.crawler.registry.npm import (
    npm_search_url,
    parse_npm_search,
)
from libref.services.discovery import DiscoveryCandidate, rank_candidates


def _candidate(name: str, **overrides) -> DiscoveryCandidate:
    base = {
        "ecosystem": "npm",
        "name": name,
        "version": "1.0",
        "description": "",
        "popularity": 0.0,
        "updated": "",
    }
    base.update(overrides)
    return DiscoveryCandidate(**base)


def test_npm_search_url_encodes_query(monkeypatch, tmp_path) -> None:
    url = npm_search_url("react hooks", 5)
    assert url.startswith("https://registry.npmjs.org/-/v1/search?")
    assert "text=react+hooks" in url or "text=react%20hooks" in url
    assert "size=5" in url


def test_parse_npm_search_flattens_objects(monkeypatch, tmp_path) -> None:
    payload = {
        "objects": [
            {
                "package": {
                    "name": "react",
                    "version": "19.3.1",
                    "description": "UI library",
                    "date": "2026-10-14T16:47:15.290Z",
                    "links": {
                        "homepage": "https://react.dev/",
                        "repository": "https://github.com/facebook/react",
                    },
                },
                "score": {"final": 2379.0, "detail": {}},
                "downloads": {"monthly": 865843985},
                "searchScore": 2379.0,
            },
            {"package": {}, "score": {}},
        ]
    }
    items = parse_npm_search(payload)
    assert len(items) == 1
    assert items[0]["name"] == "react"
    assert items[0]["popularity"] == 865843985
    assert items[0]["position"] == 0


def test_maven_search_url_targets_solr(monkeypatch, tmp_path) -> None:
    url = maven_search_url("group:artifact", 3)
    assert url.startswith("https://search.maven.org/solrsearch/select?")
    assert "wt=json" in url
    assert "rows=3" in url


def test_parse_maven_search_builds_coordinates(monkeypatch, tmp_path) -> None:
    payload = {
        "response": {
            "docs": [
                {
                    "g": "org.slf4j",
                    "a": "slf4j-api",
                    "latestVersion": "2.0.17",
                    "timestamp": 1730000000000,
                    "versionCount": 70,
                },
                {"g": "", "a": "broken"},
            ]
        }
    }
    items = parse_maven_search(payload)
    assert len(items) == 1
    assert items[0]["name"] == "org.slf4j:slf4j-api"
    assert items[0]["version"] == "2.0.17"
    assert items[0]["popularity"] == 70


def test_github_search_headers_bearer_only_with_token(monkeypatch, tmp_path) -> None:
    assert "Authorization" not in github_search_headers(None)
    assert "Authorization" not in github_search_headers("  ")
    assert github_search_headers("tok")["Authorization"] == "Bearer tok"
    assert "per_page=" in github_search_url("react", 200)


def test_parse_github_search_keeps_docs_like_homepage(monkeypatch, tmp_path) -> None:
    payload = {
        "items": [
            {
                "full_name": "facebook/react",
                "description": "UI library",
                "homepage": "https://react.dev/",
                "html_url": "https://github.com/facebook/react",
                "stargazers_count": 200000,
                "updated_at": "2026-11-10T07:13:42Z",
                "default_branch": "main",
            },
            {
                "full_name": "o/r",
                "homepage": "https://github.com/o/r",
                "html_url": "https://github.com/o/r",
            },
            {},
        ]
    }
    items = parse_github_search(payload)
    assert len(items) == 2
    assert items[0]["homepage"] == "https://react.dev/"
    assert items[0]["popularity"] == 200000
    assert items[1]["homepage"] is None
    assert items[1]["repo_url"] == "https://github.com/o/r"


def test_rank_docs_availability_first(monkeypatch, tmp_path) -> None:
    without_docs = _candidate("react", popularity=999.0)
    with_docs = _candidate("react-dom", docs_url="https://react.dev/", popularity=1.0)
    ranked = rank_candidates([without_docs, with_docs], "react")
    assert ranked[0].name == "react-dom"


def test_rank_exact_match_before_popular(monkeypatch, tmp_path) -> None:
    popular = _candidate("react-dom", popularity=999.0)
    exact = _candidate("React", docs_url="https://react.dev/", popularity=1.0)
    ranked = rank_candidates([popular, exact], "react")
    assert ranked[0].name == "React"


def test_rank_native_order_beats_popularity(monkeypatch, tmp_path) -> None:
    low = _candidate("a-one", docs_url="https://x.dev/", popularity=1.0, rank=0)
    high = _candidate("a-two", docs_url="https://x.dev/", popularity=900.0, rank=1)
    ranked = rank_candidates([high, low], "zzz")
    assert [c.name for c in ranked] == ["a-one", "a-two"]


def test_rank_popularity_breaks_cross_source_tie(monkeypatch, tmp_path) -> None:
    npm = _candidate("n-one", docs_url="https://x.dev/", popularity=1.0, rank=0)
    maven = _candidate(
        "m-one",
        ecosystem="maven",
        docs_url="https://y.dev/",
        popularity=900.0,
        rank=0,
    )
    ranked = rank_candidates([npm, maven], "zzz")
    assert [c.name for c in ranked] == ["m-one", "n-one"]


def test_canonical_ecosystem_aliases() -> None:
    from libref.crawler.registry import canonical_ecosystem

    assert canonical_ecosystem("py") == "pypi"
    assert canonical_ecosystem("js") == "npm"
    assert canonical_ecosystem("java") == "maven"
    assert canonical_ecosystem("pypi") == "pypi"
    assert canonical_ecosystem(None) is None
    assert canonical_ecosystem("cargo") is None


def test_pypi_search_parses_project_anchors() -> None:
    from libref.crawler.registry.pypi import parse_pypi_search_html, pypi_search_url

    assert "q=" in pypi_search_url("requests")
    html = (
        '<a class="package-snippet" href="/project/requests/">'
        '<span class="package-snippet__name">requests</span>'
        '<span class="package-snippet__version">2.32.0</span>'
        '<p class="package-snippet__description">HTTP library</p></a>'
        '<a href="/project/requests-toolbelt/">toolbelt</a>'
        '<a href="/project/requests/">duplicate</a>'
    )
    items = parse_pypi_search_html(html)
    assert [i["name"] for i in items] == ["requests", "requests-toolbelt"]
    assert items[0]["version"] == "2.32.0"
    assert items[0]["description"] == "HTTP library"
    assert parse_pypi_search_html("") == []
    assert parse_pypi_search_html("<not html") == []


def test_npm_search_follows_explicit_mirror(monkeypatch, tmp_path) -> None:
    from libref.crawler.registry.npm import npm_search_url_for
    from tests.conftest import make_discovery_settings

    default = make_discovery_settings(monkeypatch, tmp_path)
    assert npm_search_url_for(default, "react", 5).startswith(
        "https://registry.npmjs.org/-/v1/search?"
    )
    cn = make_discovery_settings(
        monkeypatch,
        tmp_path,
        registry_mirror="cn",
    )
    assert npm_search_url_for(cn, "react", 5).startswith(
        "https://registry.npmmirror.com/-/v1/search?"
    )
