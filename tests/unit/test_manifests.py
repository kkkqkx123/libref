"""Manifest parsing tests: identity and dependency extraction for go, cargo, and csproj."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_go_module_and_requires(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "go.mod",
        "module github.com/acme/demo\n\ngo 1.21\n\nrequire (\n"
        "\tgithub.com/foo/bar v1.2.3\n"
        "\tgolang.org/x/tools v0.1.0 // indirect\n"
        ")\n",
    )
    assert manifest_identity(tmp_path) == ("go", "github.com/acme/demo")
    deps = manifest_dependencies(tmp_path)
    assert [(d.ecosystem, d.name, d.req) for d in deps] == [
        ("go", "github.com/foo/bar", "v1.2.3"),
        ("go", "golang.org/x/tools", "v0.1.0"),
    ]
    assert all(d.source_file == "go.mod" for d in deps)


def test_go_single_require_and_unparsable_module(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "go.mod",
        "module example.com/demo\nrequire example.com/other v0.0.1\n",
    )
    assert manifest_identity(tmp_path) == ("go", "example.com/demo")
    assert [(d.name, d.req) for d in manifest_dependencies(tmp_path)] == [
        ("example.com/other", "v0.0.1")
    ]
    _write(tmp_path, "go.mod", "not a module line\n")
    assert manifest_identity(tmp_path) is None
    assert manifest_dependencies(tmp_path) == []


def test_cargo_package_and_requires(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "Cargo.toml",
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[dependencies]\nserde = "1.0"\n'
        'tokio = { version = "1.2", features = ["full"] }\n\n'
        '[target."cfg(windows)".dependencies]\nwinapi = "0.3"\n',
    )
    assert manifest_identity(tmp_path) == ("crates", "demo")
    assert [(d.ecosystem, d.name, d.req) for d in manifest_dependencies(tmp_path)] == [
        ("crates", "serde", "1.0"),
        ("crates", "tokio", "1.2"),
        ("crates", "winapi", "0.3"),
    ]


def test_cargo_invalid_toml_yields_nothing(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(tmp_path, "Cargo.toml", "[package\nname = \n")
    assert manifest_identity(tmp_path) is None
    assert manifest_dependencies(tmp_path) == []


def test_csproj_identity_and_requires(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "Demo.csproj",
        '<Project Sdk="Microsoft.NET.Sdk">\n'
        "  <PropertyGroup><PackageId>Acme.Demo</PackageId></PropertyGroup>\n"
        "  <ItemGroup>\n"
        '    <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />\n'
        '    <PackageReference Include="Serilog">\n'
        "      <Version>2.12.0</Version>\n"
        "    </PackageReference>\n"
        "  </ItemGroup>\n"
        "</Project>\n",
    )
    assert manifest_identity(tmp_path) == ("nuget", "acme.demo")
    deps = manifest_dependencies(tmp_path)
    assert [(d.ecosystem, d.name, d.req, d.source_file) for d in deps] == [
        ("nuget", "newtonsoft.json", "13.0.1", "Demo.csproj"),
        ("nuget", "serilog", "2.12.0", "Demo.csproj"),
    ]


def test_manifest_identity_priority_and_empty(tmp_path) -> None:
    from libref.astminer.manifests import manifest_identity

    assert manifest_identity(tmp_path) is None
    _write(tmp_path, "Cargo.toml", '[package]\nname = "demo"\n')
    assert manifest_identity(tmp_path) == ("crates", "demo")
    _write(tmp_path, "go.mod", "module example.com/demo\n")
    assert manifest_identity(tmp_path) == ("go", "example.com/demo")


def test_normalize_dep_name() -> None:
    from libref.astminer.manifests import normalize_dep_name

    assert normalize_dep_name("go", "github.com/Acme/Demo") == "github.com/Acme/Demo"
    assert normalize_dep_name("crates", "Serde") == "serde"
    assert normalize_dep_name("nuget", "Newtonsoft.Json") == "newtonsoft.json"
    assert normalize_dep_name("pypi", "Acme_Demo") == "acme-demo"
    assert normalize_dep_name("npm", "Lodash") == "lodash"
    assert normalize_dep_name("maven", "org.demo:lib") == "org.demo:lib"
    assert normalize_dep_name("maven", "broken") == ""
    assert normalize_dep_name("custom", "X") == "X"


def test_package_json_identity_and_requires(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "package.json",
        '{"name": "@acme/demo", "dependencies": {"lodash": "^4.17.21"},'
        ' "devDependencies": {"jest": "29.0.0"}}',
    )
    assert manifest_identity(tmp_path) == ("npm", "@acme/demo")
    assert [(d.ecosystem, d.name, d.req) for d in manifest_dependencies(tmp_path)] == [
        ("npm", "lodash", "4.17.21"),
        ("npm", "jest", "29.0.0"),
    ]
    assert all(d.source_file == "package.json" for d in manifest_dependencies(tmp_path))
    _write(tmp_path, "package.json", "not json")
    assert manifest_identity(tmp_path) is None
    assert manifest_dependencies(tmp_path) == []


def test_pyproject_and_requirements_requires(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "Acme-Demo"\ndependencies = ["requests>=2", "click"]\n',
    )
    assert manifest_identity(tmp_path) == ("pypi", "acme-demo")
    deps = manifest_dependencies(tmp_path)
    assert [(d.ecosystem, d.name) for d in deps] == [
        ("pypi", "requests"),
        ("pypi", "click"),
    ]
    assert all(d.source_file == "pyproject.toml" for d in deps)
    _write(tmp_path, "pyproject.toml", "[project\nname = \n")
    assert manifest_identity(tmp_path) is None
    _write(tmp_path, "requirements.txt", "requests>=2\n# comment\nclick\n")
    assert [(d.name, d.req) for d in manifest_dependencies(tmp_path)] == [
        ("requests", ">=2"),
        ("click", ""),
    ]


def test_pom_identity_and_requires(tmp_path) -> None:
    from libref.astminer.manifests import manifest_dependencies, manifest_identity

    _write(
        tmp_path,
        "pom.xml",
        "<project>"
        "<groupId>org.demo</groupId><artifactId>lib</artifactId>"
        "<dependencies>"
        "<dependency><groupId>junit</groupId><artifactId>junit</artifactId>"
        "<version>4.13</version></dependency>"
        "<dependency><groupId>org.demo</groupId><artifactId>plain</artifactId>"
        "</dependency></dependencies></project>",
    )
    assert manifest_identity(tmp_path) == ("maven", "org.demo:lib")
    deps = manifest_dependencies(tmp_path)
    assert [(d.ecosystem, d.name, d.req) for d in deps] == [
        ("maven", "junit:junit", "4.13"),
        ("maven", "org.demo:plain", ""),
    ]
    assert all(d.source_file == "pom.xml" for d in deps)
    _write(tmp_path, "pom.xml", "<project><broken")
    assert manifest_identity(tmp_path) is None
    assert manifest_dependencies(tmp_path) == []
