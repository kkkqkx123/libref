"""Manifest dependency edges: end to end from repo manifests to version-scoped rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from libref.services.code.code_ingest import index_repo, list_repo_dependencies
from tests.conftest import copy_tri_repo

pytestmark = pytest.mark.integration


def _add_manifests(repo: Path) -> None:
    go_mod = repo / "go.mod"
    go_mod.write_text(
        go_mod.read_text(encoding="utf-8")
        + "\nrequire (\n\tgithub.com/foo/bar v1.2.3\n\tgolang.org/x/tools v0.1.0\n)\n",
        encoding="utf-8",
    )
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[dependencies]\nserde = "1.0"\n'
        'tokio = { version = "1.2" }\n',
        encoding="utf-8",
    )
    (repo / "Demo.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        "  <ItemGroup>\n"
        '    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />\n'
        "  </ItemGroup>\n"
        "</Project>\n",
        encoding="utf-8",
    )
    (repo / "gradle").mkdir(exist_ok=True)
    (repo / "gradle" / "libs.versions.toml").write_text(
        '[versions]\njunit = "5.0"\n'
        "[libraries]\n"
        'junit = { module = "org.junit:junit", version.ref = "junit" }\n',
        encoding="utf-8",
    )
    (repo / "build.gradle").write_text(
        "dependencies {\n"
        "    implementation 'org.demo:lib:1.0.0'\n"
        "    testImplementation(libs.junit)\n"
        "}\n",
        encoding="utf-8",
    )


def test_manifest_deps_persisted_and_refreshed(conn, tmp_path) -> None:
    repo = copy_tri_repo(tmp_path)
    _add_manifests(repo)
    counts = index_repo(
        conn, "tri-deps", "0.1", str(repo), repos_dir=tmp_path / "repos"
    )
    assert counts["manifest_deps"] == 7

    deps = list_repo_dependencies(conn, "tri-deps", "0.1")
    assert [(d["ecosystem"], d["name"], d["req"]) for d in deps] == [
        ("crates", "serde", "1.0"),
        ("crates", "tokio", "1.2"),
        ("go", "github.com/foo/bar", "v1.2.3"),
        ("go", "golang.org/x/tools", "v0.1.0"),
        ("maven", "org.demo:lib", "1.0.0"),
        ("maven", "org.junit:junit", "5.0"),
        ("nuget", "newtonsoft.json", "13.0.1"),
    ]
    assert {d["source_file"] for d in deps} == {
        "go.mod",
        "Cargo.toml",
        "Demo.csproj",
        "build.gradle",
    }

    scoped = list_repo_dependencies(conn, "tri-deps", "0.1", "go")
    assert [d["name"] for d in scoped] == ["github.com/foo/bar", "golang.org/x/tools"]
    assert list_repo_dependencies(conn, "tri-deps", "0.1", "pypi") == []

    second = index_repo(
        conn, "tri-deps", "0.1", str(repo), repos_dir=tmp_path / "repos"
    )
    assert second["manifest_deps"] == 7
    assert len(list_repo_dependencies(conn, "tri-deps", "0.1")) == 7


def test_repo_without_manifests_yields_no_deps(conn, tmp_path) -> None:
    repo = copy_tri_repo(tmp_path)
    (repo / "go.mod").unlink()
    counts = index_repo(
        conn, "tri-nodeps", "0.1", str(repo), repos_dir=tmp_path / "repos"
    )
    assert counts["manifest_deps"] == 0
    assert list_repo_dependencies(conn, "tri-nodeps", "0.1") == []
