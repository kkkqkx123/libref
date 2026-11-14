"""Fixture naming and identity derivation, the part of the archive a human reads first.

A source fixture names the ref it was taken at and derives its version from that
ref, so a directory listing says whether the payload is a repository head or a
fixed release. Wrong derivation would silently point judgments, floors and the
dataset scope at a different corpus, so it is pinned here without touching any
archive.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.bench import archive as bench_archive

pytestmark = pytest.mark.unit


def _source(**overrides) -> dict:
    entry = {
        "project": "demo",
        "kind": "code",
        "library": "demo",
        "ecosystem": "pypi",
        "repo_url": "https://example.invalid/demo",
        "subdir": "src",
    }
    entry.update(overrides)
    if "version" not in entry:
        entry["version"] = bench_archive.ref_label(entry)
    return entry


def _spec(directory: Path, entry: dict) -> Path:
    path = directory / "bench.json"
    path.write_text(json.dumps({"fixtures": [entry]}), encoding="utf-8")
    return path


def test_source_fixture_following_the_branch_is_labelled_head():
    entry = _source()
    assert bench_archive.fixture_id(entry) == "demo/code-head"
    assert bench_archive.archive_dir(entry).name == "code-head"
    assert entry["version"] == bench_archive.HEAD_REF


def test_source_fixture_pinned_to_a_tag_names_the_tag():
    entry = _source(ref="v1.2.3")
    assert bench_archive.fixture_id(entry) == "demo/code-v1.2.3"
    assert bench_archive.identity(entry)["version"] == "v1.2.3"
    assert bench_archive.recipe(entry)["ref"] == "v1.2.3"


def test_two_refs_of_one_repository_do_not_collide():
    head = _source()
    tag = _source(ref="v1.2.3")
    assert bench_archive.fixture_id(head) != bench_archive.fixture_id(tag)


def _loadable(entry):
    entry = dict(entry)
    # The version of a source fixture follows from its ref, so it is derived
    # by load_spec instead of written into the spec.
    if entry.get("kind") == "code":
        entry.pop("version", None)
    return entry


def test_tier_defaults_to_main_and_rejects_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(bench_archive, "SPEC", tmp_path / "bench.json")
    entry = {
        "project": "demo",
        "kind": "docs",
        "library": "demo",
        "ecosystem": "pypi",
        "version": "docs",
        "start_url": "https://example.invalid/",
        "doc_version": "unversioned",
    }
    entry = _loadable(entry)
    _spec(tmp_path, dict(entry))
    assert bench_archive.load_spec()[0]["tier"] == "main"
    _spec(tmp_path, dict(entry, tier="pool"))
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()
    _spec(tmp_path, dict(entry, tier="archive"))
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()


def test_pool_fixtures_only_supply_source_material(monkeypatch, tmp_path):
    monkeypatch.setattr(bench_archive, "SPEC", tmp_path / "bench.json")
    _spec(tmp_path, _loadable(_source(tier="pool")))
    assert bench_archive.load_spec()[0]["tier"] == "pool"
    _spec(
        tmp_path,
        {
            "project": "demo",
            "kind": "docs",
            "library": "demo",
            "ecosystem": "pypi",
            "version": "docs",
            "start_url": "https://example.invalid/",
            "tier": "pool",
        },
    )
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()


def test_budgets_must_be_positive_and_code_only_for_files(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(bench_archive, "SPEC", tmp_path / "bench.json")
    _spec(tmp_path, _loadable(_source(max_files=100, max_build_ms=1000)))
    assert bench_archive.load_spec()[0]["max_files"] == 100
    _spec(tmp_path, _loadable(_source(max_files=0)))
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()
    _spec(
        tmp_path,
        {
            "project": "demo",
            "kind": "docs",
            "library": "demo",
            "ecosystem": "pypi",
            "version": "docs",
            "start_url": "https://example.invalid/",
            "max_files": 10,
        },
    )
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()


def test_document_fixture_keeps_its_plain_name():
    entry = {
        "project": "demo",
        "kind": "docs",
        "library": "demo",
        "ecosystem": "pypi",
        "version": "docs",
        "start_url": "https://example.invalid/",
    }
    assert bench_archive.fixture_id(entry) == "demo/docs"
    assert bench_archive.archive_dir(entry).name == "docs"


@pytest.mark.parametrize("ref", ["a/b", "a\\b", "a b", "a:b", "..", "."])
def test_refs_that_would_distort_the_path_are_rejected(ref):
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.ref_label({"kind": "code", "ref": ref})


def test_spec_rejects_a_hand_written_version_on_a_source_fixture(monkeypatch, tmp_path):
    spec = _spec(tmp_path, _source(version="head"))
    monkeypatch.setattr(bench_archive, "SPEC", spec)
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()


def test_spec_derives_the_version_from_the_ref(monkeypatch, tmp_path):
    entry = _source(ref="v9")
    del entry["version"]
    monkeypatch.setattr(bench_archive, "SPEC", _spec(tmp_path, entry))
    entries = bench_archive.load_spec()
    assert entries[0]["version"] == "v9"
    assert bench_archive.fixture_id(entries[0]) == "demo/code-v9"


def _page(
    url: str, body: str = "<html><body><main><p>x</p></main></body></html>"
) -> dict:
    return {"url": url, "body": body}


def test_version_segment_of_the_crawled_paths_is_the_evidence():
    pages = [
        _page("https://docs.example.invalid/en/2.9/usage/"),
        _page("https://docs.example.invalid/en/2.9/api/"),
    ]
    evidence = bench_archive.doc_version_evidence(pages)
    assert evidence["value"] == "2.9"
    assert evidence["signal"] == "url-path[1]"


def test_documentation_stream_names_are_kept_verbatim():
    pages = [
        _page("https://docs.example.invalid/en/latest/usage/"),
        _page("https://docs.example.invalid/en/latest/api/"),
    ]
    assert bench_archive.doc_version_evidence(pages)["value"] == "latest"


def test_canonical_names_the_version_when_paths_do_not():
    canonical = "https://docs.example.invalid/en/12.2.0/usage/"
    body = f'<html><head><link rel="canonical" href="{canonical}"></head><body><p>x</p></body></html>'
    pages = [
        _page("https://docs.example.invalid/usage/", body),
        _page("https://docs.example.invalid/api/", body),
    ]
    evidence = bench_archive.doc_version_evidence(pages)
    assert evidence["value"] == "12.2.0"
    assert evidence["signal"] == "canonical"


def test_published_metadata_names_a_version_band():
    body = '<html><head><meta name="docsearch:version" content="v3.x"></head><body><p>x</p></body></html>'
    pages = [
        _page("https://docs.example.invalid/usage/", body),
        _page("https://docs.example.invalid/api/", body),
    ]
    evidence = bench_archive.doc_version_evidence(pages)
    assert evidence["value"] == "v3.x"
    assert evidence["signal"] == "meta:docsearch:version"


def test_a_site_that_states_nothing_is_marked_unversioned():
    pages = [
        _page("https://docs.example.invalid/usage/"),
        _page("https://docs.example.invalid/api/"),
    ]
    evidence = bench_archive.doc_version_evidence(pages)
    assert evidence == {
        "value": bench_archive.UNVERSIONED,
        "signal": "none",
        "sample_url": "",
    }


def test_crawled_paths_win_over_a_canonical_that_names_another_stream():
    canonical = "https://docs.example.invalid/en/stable/usage/"
    body = f'<html><head><link rel="canonical" href="{canonical}"></head><body><p>x</p></body></html>'
    pages = [
        _page("https://docs.example.invalid/en/latest/usage/", body),
        _page("https://docs.example.invalid/en/latest/api/", body),
    ]
    assert bench_archive.doc_version_evidence(pages)["value"] == "latest"


def test_a_declared_version_must_match_the_observation():
    evidence = {"value": "latest", "signal": "url-path[1]", "sample_url": "u"}
    bench_archive.check_doc_version("latest", evidence)
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.check_doc_version("3.7.0", evidence)


def test_unversioned_must_be_declared_explicitly():
    evidence = {"value": bench_archive.UNVERSIONED, "signal": "none", "sample_url": ""}
    bench_archive.check_doc_version(bench_archive.UNVERSIONED, evidence)
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.check_doc_version("1.0.0", evidence)


def test_spec_requires_a_document_version(monkeypatch, tmp_path):
    entry = {
        "project": "demo",
        "kind": "docs",
        "library": "demo",
        "ecosystem": "pypi",
        "version": "docs",
        "start_url": "https://example.invalid/",
    }
    monkeypatch.setattr(bench_archive, "SPEC", _spec(tmp_path, entry))
    with pytest.raises(bench_archive.ArchiveError):
        bench_archive.load_spec()


def test_source_payload_keeps_only_what_the_spec_indexes(tmp_path):
    """Excluded entries never reach the payload, and the declared exclusions are part of the recipe."""
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "impl.go").write_text("package pkg\n", encoding="utf-8")
    (tree / "pkg" / "impl_test.go").write_text("package pkg\n", encoding="utf-8")
    payload = tmp_path / "source.tar.gz"
    stats = bench_archive.write_source(
        payload, tree, prefix="pkg/", exclude=("*_test.go",)
    )
    assert stats["files"] == 1
    extracted = bench_archive.extract_source(payload, tmp_path / "out")
    assert sorted(path.name for path in extracted.rglob("*") if path.is_file()) == [
        "impl.go"
    ]


def test_recipe_records_the_exclusions(monkeypatch, tmp_path):
    entry = _source(exclude_globs=["*_test.go"])
    entry.pop("version")
    monkeypatch.setattr(bench_archive, "SPEC", _spec(tmp_path, entry))
    loaded = bench_archive.load_spec()[0]
    assert bench_archive.recipe(loaded)["exclude_globs"] == ["*_test.go"]
