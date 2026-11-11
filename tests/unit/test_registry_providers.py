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


def test_go_repo_url_prefers_origin_then_host_path() -> None:
    from libref.crawler.registry.go import infer_go_repo_url, parse_go_repo_url

    payload = {"Origin": {"URL": "https://github.com/spf13/cobra"}}
    assert (
        parse_go_repo_url(payload, "github.com/spf13/cobra")
        == "https://github.com/spf13/cobra"
    )
    assert (
        parse_go_repo_url({}, "github.com/spf13/cobra")
        == "https://github.com/spf13/cobra"
    )
    assert (
        parse_go_repo_url({}, "github.com/spf13/cobra/v2")
        == "https://github.com/spf13/cobra"
    )
    assert parse_go_repo_url({}, "golang.org/x/text") is None
    assert parse_go_repo_url({}, "gopkg.in/yaml.v3") is None
    assert infer_go_repo_url("https://github.com/a/b") is None
    assert infer_go_repo_url("github.com/a") is None


def test_nuget_nuspec_repo_and_project_promotion() -> None:
    from libref.crawler.registry.nuget import (
        nuget_nuspec_url,
        nuget_repo_from_project_url,
        parse_nuget_nuspec,
    )

    assert nuget_nuspec_url("Newtonsoft.Json", "13.0.3").endswith(
        "/newtonsoft.json/13.0.3/newtonsoft.json.nuspec"
    )
    assert (
        nuget_repo_from_project_url("https://github.com/acme/demo")
        == "https://github.com/acme/demo"
    )
    assert nuget_repo_from_project_url("https://demo.dev/") is None
    assert nuget_repo_from_project_url(None) is None
    nuspec = (
        '<package><metadata><repository type="git" '
        'url="https://github.com/acme/demo" /></metadata></package>'
    )
    assert parse_nuget_nuspec(nuspec) == "https://github.com/acme/demo"
    assert parse_nuget_nuspec("<package></package>") is None
    assert parse_nuget_nuspec("not xml") is None


def test_normalize_name_dispatch(monkeypatch, tmp_path) -> None:
    from libref.crawler.registry import normalize_name

    assert normalize_name("go", "github.com/acme/demo") == "github.com/acme/demo"
    assert normalize_name("golang", "github.com/acme/demo") == "github.com/acme/demo"
    assert normalize_name("crates", "Serde") == "serde"
    assert normalize_name("cargo", "Serde") == "serde"
    assert normalize_name("nuget", "Newtonsoft.Json") == "newtonsoft.json"
    assert normalize_name("dotnet", "Newtonsoft.Json") == "newtonsoft.json"


def test_next_actions_for_docs_and_repo() -> None:
    from libref.services.registry.resolver import next_actions_for

    docs = next_actions_for("pypi", "requests", "docs", "no_docs")
    assert docs[0]["kind"] == "tool"
    assert docs[0]["mcp_tool"] == "library_search"
    assert "discover" in docs[0]["cli"]
    assert docs[-1]["kind"] == "manual" and "ingest" in docs[-1]["detail"]
    repo = next_actions_for("go", "github.com/acme/demo", "repo", "no_docs")
    assert "index-repo" in repo[-1]["detail"]
    retry = next_actions_for(None, "demo", "docs", "retryable")
    assert "--ecosystem" not in retry[0]["cli"]
    assert any(a["kind"] == "manual" and "retry" in a["detail"] for a in retry)


def test_format_registry_error_renders_guidance() -> None:
    from libref.cli.common import format_registry_error
    from libref.crawler.registry import RegistryError

    exc = RegistryError(
        "no_docs",
        "registry has no repository address",
        hint={
            "ecosystem": "go",
            "name": "github.com/acme/demo",
            "homepage": "https://demo.dev/",
        },
    )
    text = format_registry_error(exc, target="repo")
    assert "no_docs" in text and "go" in text
    assert "libref discover" in text and "index-repo" in text


DDG_FIXTURE = (
    '<div class="result">'
    '<h2 class="result__title">'
    '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Facme%2Fdemo&amp;rut=x">acme/demo: demo tool</a>'
    "</h2>"
    '<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Facme%2Fdemo&amp;rut=x">demo snippet</a>'
    "</div>"
    '<div class="result">'
    '<a class="result__a" href="https://demo.readthedocs.io/">demo docs</a>'
    "</div>"
    '<div class="result">no anchor here</div>'
)


def test_websearch_url_target_name_and_classify() -> None:
    from libref.crawler.websearch import (
        candidate_name,
        classify_target,
        extract_target,
        parse_ddg_html,
        web_search_url,
    )

    assert "q=" in web_search_url("acme demo")
    assert extract_target("https://demo.dev/") == "https://demo.dev/"
    assert (
        extract_target("//duckduckgo.com/l/?uddg=https%3A%2F%2Fdemo.dev%2F&rut=x")
        == "https://demo.dev/"
    )
    assert extract_target("/relative/path") is None
    assert extract_target(None) is None
    assert candidate_name("https://github.com/acme/demo", "t") == "acme/demo"
    assert candidate_name("https://demo.dev/guide/", "t") == "demo.dev"
    assert classify_target("https://github.com/acme/demo") == "repo"
    assert classify_target("https://demo.readthedocs.io/") == "docs"
    assert classify_target("https://example.com/blog/") == "other"
    hits = parse_ddg_html(DDG_FIXTURE)
    assert [h["url"] for h in hits] == [
        "https://github.com/acme/demo",
        "https://demo.readthedocs.io/",
    ]
    assert hits[0]["title"] == "acme/demo: demo tool"
    assert hits[0]["snippet"] == "demo snippet"
    assert parse_ddg_html("") == []


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
