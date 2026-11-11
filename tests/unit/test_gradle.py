"""Gradle dependency edges: build-script calls plus version-catalog join, Maven coordinates out."""

from __future__ import annotations

import pytest

from libref.astminer import gradle as _gradle

pytestmark = pytest.mark.unit


def _write_catalog(root, libraries: str, versions: str = "") -> None:
    catalog_dir = root / "gradle"
    catalog_dir.mkdir(exist_ok=True)
    (catalog_dir / "libs.versions.toml").write_text(
        f"[versions]\n{versions}\n[libraries]\n{libraries}\n", encoding="utf-8"
    )


def test_groovy_string_platform_project_and_custom_config(tmp_path) -> None:
    (tmp_path / "build.gradle").write_text(
        "plugins {\n    id 'java'\n}\n"
        "dependencies {\n"
        "    implementation 'org.demo:lib:1.0.0'\n"
        '    testImplementation("org.demo:testlib")\n'
        "    myCustomImplementation 'org.demo:custom:2.0'\n"
        "    implementation platform('org.demo:bom:3.0')\n"
        "    implementation project(':sibling')\n"
        "    implementation files('libs/local.jar')\n"
        "    notAConfig 'org.demo:other:9.9'\n"
        "}\n",
        encoding="utf-8",
    )
    deps = _gradle.gradle_dependencies(tmp_path)
    assert [(d.ecosystem, d.name, d.req) for d in deps] == [
        ("maven", "org.demo:lib", "1.0.0"),
        ("maven", "org.demo:testlib", ""),
        ("maven", "org.demo:custom", "2.0"),
        ("maven", "org.demo:bom", "3.0"),
    ]
    assert {d.source_file for d in deps} == {"build.gradle"}


def test_kotlin_catalog_alias_and_version_ref(tmp_path) -> None:
    _write_catalog(
        tmp_path,
        'junit = { module = "org.junit:junit", version.ref = "junit" }\n'
        'plain = { module = "org.demo:plain", version = "4.0" }\n',
        'junit = "5.0"\n',
    )
    (tmp_path / "build.gradle.kts").write_text(
        "dependencies {\n"
        "    testImplementation(libs.junit)\n"
        "    implementation(libs.plain)\n"
        '    implementation("org.demo:direct:1.2")\n'
        "}\n",
        encoding="utf-8",
    )
    deps = _gradle.gradle_dependencies(tmp_path)
    assert [(d.ecosystem, d.name, d.req) for d in deps] == [
        ("maven", "org.junit:junit", "5.0"),
        ("maven", "org.demo:plain", "4.0"),
        ("maven", "org.demo:direct", "1.2"),
    ]


def test_unresolvable_alias_and_broken_script_yield_nothing(tmp_path) -> None:
    (tmp_path / "build.gradle").write_text(
        "dependencies {\n"
        "    testImplementation libs.junit\n"
        "    implementation 'broken\n"
        "}\n",
        encoding="utf-8",
    )
    assert _gradle.gradle_dependencies(tmp_path) == []


def test_no_build_files_yields_nothing(tmp_path) -> None:
    assert _gradle.gradle_dependencies(tmp_path) == []
