"""New-ecosystem provider tests: normalization, snapshot/search parsing, facade wiring."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_supported_ecosystems_and_aliases() -> None:
    from libref.crawler.registry import (
        DEFAULT_REGISTRY_BASES,
        ECOSYSTEM_ALIASES,
        SUPPORTED_ECOSYSTEMS,
        canonical_ecosystem,
    )

    assert SUPPORTED_ECOSYSTEMS == ("pypi", "npm", "maven", "go", "crates", "nuget")
    assert set(DEFAULT_REGISTRY_BASES) == set(SUPPORTED_ECOSYSTEMS)
    assert ECOSYSTEM_ALIASES["golang"] == "go"
    assert ECOSYSTEM_ALIASES["cargo"] == "crates"
    assert ECOSYSTEM_ALIASES["csharp"] == "nuget"
    assert canonical_ecosystem("rs") == "crates"
    assert canonical_ecosystem("c#") == "nuget"


def test_registry_base_cn_falls_back_for_new_eco(monkeypatch, tmp_path) -> None:
    from libref.crawler.registry import registry_base_for
    from tests.conftest import make_settings

    settings = make_settings(monkeypatch, tmp_path, registry_mirror="cn")
    assert registry_base_for("go", settings) == "https://goproxy.cn"
    assert registry_base_for("crates", settings) == "https://crates.io"
    assert registry_base_for("nuget", settings).startswith("https://api.nuget.org")


def test_go_normalize_and_proxy_escaping() -> None:
    from libref.crawler.registry.go import (
        go_docs_url,
        go_latest_url,
        go_proxy_path,
        normalize_go_name,
        parse_go_latest,
        parse_go_list,
    )

    assert normalize_go_name("  github.com/acme/demo  ") == "github.com/acme/demo"
    with pytest.raises(ValueError):
        normalize_go_name("   ")
    assert go_proxy_path("github.com/Acme/Demo") == "github.com/!acme/!demo"
    assert go_latest_url("https://proxy.golang.org/", "github.com/Acme/Demo").endswith(
        "github.com/!acme/!demo/@latest"
    )
    assert parse_go_latest({"Version": "v1.2.3"}) == "v1.2.3"
    assert parse_go_latest({}) is None
    assert parse_go_list("v1.0.0\nv1.2.3\n\nv1.0.0\n") == ["v1.0.0", "v1.2.3"]
    assert go_docs_url("github.com/acme/demo", "v1.2.3").endswith("/demo@v1.2.3")
    assert (
        go_docs_url("github.com/acme/demo") == "https://pkg.go.dev/github.com/acme/demo"
    )


def test_go_search_parses_module_anchors() -> None:
    from libref.crawler.registry.go import parse_go_search_html

    html = (
        '<a href="/github.com/acme/demo">demo</a>'
        '<a href="/search?q=demo">search</a>'
        '<a href="/about">about</a>'
        '<a href="/golang.org/x/tools">tools</a>'
        '<a href="/github.com/acme/demo">demo again</a>'
    )
    assert [c["name"] for c in parse_go_search_html(html)] == [
        "github.com/acme/demo",
        "golang.org/x/tools",
    ]
    assert parse_go_search_html("") == []
    assert parse_go_search_html("<not html") == []


def test_crates_normalize_and_snapshot() -> None:
    from libref.crawler.registry.crates import (
        docs_rs_url,
        normalize_crates_name,
        parse_crates_search,
        parse_crates_snapshot,
    )

    assert normalize_crates_name(" Serde ") == "serde"
    with pytest.raises(ValueError):
        normalize_crates_name("not a crate!")
    payload = {
        "crate": {
            "name": "demo",
            "description": "demo crate",
            "homepage": "https://demo.dev/",
            "repository": "https://github.com/acme/demo",
            "documentation": "https://docs.demo.dev/",
            "max_stable_version": "2.0.0",
        },
        "versions": [
            {"num": "1.0.0", "yanked": False},
            {"num": "0.9.0", "yanked": True},
            {"num": "2.0.0", "yanked": False},
        ],
    }
    stable, versions, homepage, candidates, repo = parse_crates_snapshot(payload)
    assert stable == "2.0.0"
    assert versions == ["1.0.0", "2.0.0"]
    assert homepage == "https://demo.dev/"
    assert candidates[0] == "https://docs.demo.dev/"
    assert repo == "https://github.com/acme/demo"
    assert docs_rs_url("demo", "2.0.0") == "https://docs.rs/demo/2.0.0"

    hits = parse_crates_search(
        {
            "crates": [
                {
                    "name": "demo",
                    "description": "d",
                    "max_stable_version": "2.0.0",
                    "homepage": "https://demo.dev/",
                    "repository": "https://github.com/acme/demo",
                    "documentation": None,
                    "downloads": 100,
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
    )
    assert hits[0]["name"] == "demo"
    assert hits[0]["popularity"] == 100
    assert hits[0]["position"] == 0


def test_nuget_normalize_and_registration() -> None:
    from libref.crawler.registry.nuget import (
        normalize_nuget_name,
        nuget_package_url,
        nuget_page_urls,
        parse_nuget_registration,
        parse_nuget_search,
        pick_nuget_stable,
    )

    assert normalize_nuget_name("  Newtonsoft.Json ") == "newtonsoft.json"
    with pytest.raises(ValueError):
        normalize_nuget_name("has space")
    payload = {
        "items": [
            {
                "items": [
                    {
                        "catalogEntry": {
                            "version": "1.0.0",
                            "projectUrl": "https://demo.dev/",
                        }
                    },
                    {"catalogEntry": {"version": "2.0.0-beta", "projectUrl": ""}},
                    {"catalogEntry": {"version": "2.0.0", "projectUrl": ""}},
                ]
            }
        ]
    }
    versions, homepage = parse_nuget_registration(payload)
    assert versions == ["1.0.0", "2.0.0-beta", "2.0.0"]
    assert homepage == "https://demo.dev/"
    assert pick_nuget_stable(versions) == "2.0.0"
    assert pick_nuget_stable(["1.0.0-alpha"]) == "1.0.0-alpha"
    assert pick_nuget_stable([]) is None
    assert (
        nuget_package_url("Demo", "2.0.0")
        == "https://www.nuget.org/packages/Demo/2.0.0"
    )
    assert nuget_page_urls({"items": [{"@id": "https://x/page.json"}]}) == [
        "https://x/page.json"
    ]
    assert nuget_page_urls(payload) == []

    hits = parse_nuget_search(
        {
            "data": [
                {
                    "id": "Demo",
                    "version": "2.0.0",
                    "description": "d",
                    "projectUrl": "https://demo.dev/",
                    "totalDownloads": 7,
                }
            ]
        }
    )
    assert hits[0]["name"] == "Demo"
    assert hits[0]["popularity"] == 7


def test_normalize_name_dispatch(monkeypatch, tmp_path) -> None:
    from libref.crawler.registry import normalize_name

    assert normalize_name("go", "github.com/acme/demo") == "github.com/acme/demo"
    assert normalize_name("golang", "github.com/acme/demo") == "github.com/acme/demo"
    assert normalize_name("crates", "Serde") == "serde"
    assert normalize_name("cargo", "Serde") == "serde"
    assert normalize_name("nuget", "Newtonsoft.Json") == "newtonsoft.json"
    assert normalize_name("dotnet", "Newtonsoft.Json") == "newtonsoft.json"


def test_best_effort_stable_prerelease_flag() -> None:
    from libref.crawler.registry import best_effort_stable

    versions = ["1.0.0", "2.0.0-beta", "0.9.0"]
    assert best_effort_stable(versions) == "1.0.0"
    assert best_effort_stable(versions, include_prerelease=True) == "2.0.0-beta"
    assert best_effort_stable([]) is None
    assert best_effort_stable([], include_prerelease=True) is None
    assert best_effort_stable(["1.0.0-alpha"]) == "1.0.0-alpha"


def test_pick_version_prerelease_flag() -> None:
    from libref.services.registry.resolver import _pick_version

    versions = ["1.0.0", "2.0.0-beta"]
    assert _pick_version(versions, "1.0.0", None) == ("1.0.0", True)
    assert _pick_version(versions, "1.0.0", None, True) == ("2.0.0-beta", True)
    assert _pick_version(versions, "1.0.0", "2.0.0-beta") == ("2.0.0-beta", True)
    assert _pick_version(versions, "1.0.0", "2.0.0-beta", True) == (
        "2.0.0-beta",
        True,
    )
    assert _pick_version(versions, None, None) == ("1.0.0", True)
