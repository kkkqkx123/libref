"""New-ecosystem resolution tests: offline MockTransport assertions for go, crates, and nuget."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from libref.crawler.registry import RegistryError
from libref.crawler.registry import overrides as _overrides_state
from libref.services.registry.resolver import (
    infer_ecosystem,
    resolve_package,
    resolve_repo,
)
from tests.conftest import make_registry_settings

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_file_overrides():
    _overrides_state.clear_file_overrides()
    yield
    _overrides_state.clear_file_overrides()


def run(coro):
    return asyncio.run(coro)


GO_LATEST = {
    "Version": "v1.2.3",
    "Time": "2026-01-01T00:00:00Z",
    "Origin": {"VCS": "git", "URL": "https://github.com/acme/demo"},
}
GO_LIST = "v1.0.0\nv1.2.3\n"
NUGET_NUSPEC_WITH_REPO = (
    '<package><metadata><repository type="git" '
    'url="https://github.com/acme/demo" /></metadata></package>'
)

CRATES_PAYLOAD = {
    "crate": {
        "name": "demo",
        "description": "demo crate",
        "homepage": "https://demo.dev/",
        "repository": "https://github.com/acme/demo",
        "documentation": None,
        "max_stable_version": "2.0.0",
    },
    "versions": [{"num": "1.0.0", "yanked": False}, {"num": "2.0.0", "yanked": False}],
}

NUGET_INLINE = {
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

NUGET_PAGED_INDEX = {
    "items": [
        {
            "@id": "https://api.nuget.org/v3/page/demo.json",
            "lower": "1.0.0",
            "upper": "1.0.0",
        }
    ]
}
NUGET_PAGED_PAGE = {
    "items": [
        {
            "items": [
                {
                    "catalogEntry": {
                        "version": "1.0.0",
                        "projectUrl": "https://demo.dev/",
                    }
                }
            ]
        }
    ]
}


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "proxy.golang.org":
        if path == "/github.com/acme/demo/@latest":
            return httpx.Response(200, json=GO_LATEST)
        if path == "/github.com/acme/demo/@v/list":
            return httpx.Response(200, text=GO_LIST)
        if path == "/github.com/!acme/!demo/@latest":
            return httpx.Response(200, json=GO_LATEST)
        if path == "/github.com/!acme/!demo/@v/list":
            return httpx.Response(200, text=GO_LIST)
        if path.endswith(("@latest", "@v/list")):
            return httpx.Response(404, json={"error": "not found"})
    if host == "crates.io":
        if path == "/api/v1/crates/demo":
            return httpx.Response(200, json=CRATES_PAYLOAD)
        if path.startswith("/api/v1/crates/"):
            return httpx.Response(404, json={"errors": [{"detail": "not found"}]})
    if host == "api.nuget.org":
        if path == "/v3/registration5-gz-semver2/demo/index.json":
            return httpx.Response(200, json=NUGET_INLINE)
        if path == "/v3/registration5-gz-semver2/paged/index.json":
            return httpx.Response(200, json=NUGET_PAGED_INDEX)
        if path == "/v3/page/demo.json":
            return httpx.Response(200, json=NUGET_PAGED_PAGE)
        if path == "/v3-flatcontainer/demo/2.0.0/demo.nuspec":
            return httpx.Response(200, text=NUGET_NUSPEC_WITH_REPO)
        if path.startswith("/v3-flatcontainer/"):
            return httpx.Response(404, text="not found")
        if path.endswith("/index.json"):
            return httpx.Response(404, json={})
    raise AssertionError(f"{host}{path}")


def client():
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_go_resolve_docs_and_uppercase_escape(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(resolve_package(conn, client(), settings, "go", "github.com/acme/demo"))
    assert res.ecosystem == "go"
    assert res.version == "v1.2.3"
    assert res.docs_url == "https://pkg.go.dev/github.com/acme/demo@v1.2.3"
    assert res.repo_url == "https://github.com/acme/demo"
    assert res.source == "registry"
    repo = run(resolve_repo(conn, client(), settings, "go", "github.com/acme/demo"))
    assert repo.repo_url == "https://github.com/acme/demo"
    upper = run(resolve_package(conn, client(), settings, "go", "github.com/Acme/Demo"))
    assert upper.version == "v1.2.3"
    assert upper.repo_url == "https://github.com/acme/demo"


def test_go_not_found(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    with pytest.raises(RegistryError) as excinfo:
        run(resolve_package(conn, client(), settings, "go", "example.com/missing"))
    assert excinfo.value.kind == "not_found"


def test_crates_resolve_docs_and_repo(conn, monkeypatch, tmp_path) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(resolve_package(conn, client(), settings, "crates", "Demo"))
    assert res.ecosystem == "crates"
    assert res.name == "demo"
    assert res.version == "2.0.0"
    assert res.docs_url == "https://docs.rs/demo/2.0.0"
    assert res.repo_url == "https://github.com/acme/demo"
    repo = run(resolve_repo(conn, client(), settings, "crates", "demo", "2.0.0"))
    assert repo.repo_url == "https://github.com/acme/demo"
    assert repo.source == "registry"
    with pytest.raises(RegistryError) as excinfo:
        run(resolve_package(conn, client(), settings, "crates", "missing-crate-xyz"))
    assert excinfo.value.kind == "not_found"


def test_nuget_resolve_prefers_stable_and_project_url(
    conn, monkeypatch, tmp_path
) -> None:
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(resolve_package(conn, client(), settings, "nuget", "Demo"))
    assert res.ecosystem == "nuget"
    assert res.name == "demo"
    assert res.version == "2.0.0"
    assert res.docs_url == "https://demo.dev/"
    assert "https://www.nuget.org/packages/demo/2.0.0" in res.candidates
    assert res.repo_url == "https://github.com/acme/demo"
    repo = run(resolve_repo(conn, client(), settings, "nuget", "demo"))
    assert repo.repo_url == "https://github.com/acme/demo"
    paged = run(resolve_package(conn, client(), settings, "nuget", "paged"))
    assert paged.version == "1.0.0"
    assert paged.docs_url == "https://demo.dev/"


def test_infer_ecosystem_new_shapes() -> None:
    assert infer_ecosystem("golang", "github.com/a/b") == "go"
    assert infer_ecosystem("cargo", "serde") == "crates"
    assert infer_ecosystem("dotnet", "Newtonsoft.Json") == "nuget"
    assert infer_ecosystem(None, "github.com/a/b") == "go"
    assert infer_ecosystem("go", "github.com/a/b") == "go"


def test_file_override_wins_for_new_eco(conn, monkeypatch, tmp_path) -> None:
    _overrides_state.set_file_overrides(
        {
            ("go", "github.com/acme/demo"): {
                "docs_url": "https://custom.dev/go-demo",
                "source": "file",
            }
        }
    )
    settings = make_registry_settings(monkeypatch, tmp_path)
    res = run(
        resolve_package(
            conn, client(), settings, "go", "github.com/acme/demo", "v9.9.9"
        )
    )
    assert res.docs_url == "https://custom.dev/go-demo"
    assert res.source == "override"
