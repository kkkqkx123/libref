"""Source-aware usage mining: implementation isolation, snippets and target filters."""

from __future__ import annotations

import pytest

from libref.astminer.code_blocks import fenced_blocks, rust_doc_blocks
from libref.astminer.models import CodeSnippet
from libref.services.code.code_ingest import index_repo
from libref.services.code.mining_refresh import mine_patterns
from libref.services.code.usage_ingest import ingest_snippets
from tests.conftest import copy_mini_repo

pytestmark = pytest.mark.integration


def test_usage_profile_isolated_from_implementation(conn, tmp_path):
    repo = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(repo), repos_dir=tmp_path / "repos")
    assert mine_patterns(conn, "demo", "0.1", minsup=2) == []

    snippets = [
        CodeSnippet(
            sample_id="example-1.py",
            language="python",
            code=("import numpy as np\na = np.array([1, 2])\nb = np.zeros(2)\n"),
        ),
        CodeSnippet(
            sample_id="example-2.py",
            language="python",
            code=("import numpy as np\na = np.array([3, 4])\nb = np.zeros(2)\n"),
        ),
    ]
    out = ingest_snippets(
        conn,
        "demo",
        "0.1",
        snippets,
        source_key="examples:demo",
        source_role="official_example",
        target_filter={"prefixes": ["numpy."]},
    )
    assert out["calls"] >= 4
    results = mine_patterns(conn, "demo", "0.1", minsup=2)
    assert ["numpy.array", "numpy.zeros"] in [r["pattern"] for r in results]
    assert all(all(name.startswith("numpy.") for name in r["pattern"]) for r in results)

    impl = mine_patterns(conn, "demo", "0.1", minsup=2, profile="implementation")
    assert impl
    assert any(any(name.startswith("numpy.") for name in r["pattern"]) for r in impl)


def test_reference_project_target_filter(conn, tmp_path):
    repo = copy_mini_repo(tmp_path)
    index_repo(conn, "demo", "0.1", str(repo), repos_dir=tmp_path / "repos")
    snippet = CodeSnippet(
        sample_id="transport.c",
        language="c",
        code=(
            "void process(void) {\n"
            "  mtl_setup();\n"
            "  rte_eth_rx_burst(0, 0, 0, 32);\n"
            "}\n"
        ),
    )
    ingest_snippets(
        conn,
        "demo",
        "0.1",
        [snippet],
        source_key="reference:mtl",
        source_role="reference_project",
        target_filter={"prefixes": ["rte_"]},
    )
    rows = conn.execute(
        "SELECT callee_name, source_role, in_target_api FROM call_instances"
        " WHERE source_role='reference_project'"
    ).fetchall()
    assert any(r[0] == "rte_eth_rx_burst" and int(r[2]) == 1 for r in rows)
    assert any(r[0] == "mtl_setup" and int(r[2]) == 0 for r in rows)


def test_doc_and_rust_fence_extraction(conn):
    blocks = fenced_blocks(
        "# Quickstart\n\n```python\nimport demo\ndemo.run()\n```\n\n"
        "```shell\nlibref mine\n```",
        sample_prefix="doc:1",
    )
    assert len(blocks) == 1
    assert blocks[0].language == "python"
    assert "demo.run()" in blocks[0].code

    doc_blocks = rust_doc_blocks(
        b"/// Example:\n///\n/// ```rust,no_run\n/// libref::run();\n/// ```\n",
        path="src/lib.rs",
    )
    assert len(doc_blocks) == 1
    assert doc_blocks[0].language == "rust"
    assert doc_blocks[0].origin_kind == "rustdoc_doctest"


def test_stdlib_calls_excluded_from_mining_by_default(conn):
    from libref.astminer.models import CodeSnippet
    from libref.services.registry.libraries import LibraryService

    library_id, version_id = LibraryService(conn).ensure_library_version("demo", "0.1")
    for name in ("rte_eth_rx_burst", "rte_eth_tx_burst"):
        conn.execute(
            "INSERT INTO api_entities(entity_uid, library_id, version_id,"
            " qualified_name, kind) VALUES(?, ?, ?, ?, 'function')",
            (f"uid:{name}", library_id, version_id, name),
        )
    snippets = [
        CodeSnippet(
            sample_id=f"c-{index}",
            language="c",
            code=(
                f"void use_{index}(void) {{\n"
                '  printf("x");\n'
                "  malloc(8);\n"
                "  rte_eth_rx_burst(0, 0, 0, 32);\n"
                "  rte_eth_tx_burst(0, 0, 0, 32);\n"
                "}\n"
            ),
        )
        for index in range(2)
    ]
    ingest_snippets(
        conn,
        "demo",
        "0.1",
        snippets,
        source_key="reference:stdlib",
        source_role="reference_project",
        target_filter={"prefixes": ["rte_"]},
    )
    rows = conn.execute(
        "SELECT callee_name, resolve_kind FROM call_instances"
        " WHERE source_role='reference_project'"
    ).fetchall()
    by_name = {name: kind for name, kind in rows}
    assert by_name["printf"] == "stdlib"
    assert by_name["malloc"] == "stdlib"
    patterns = [r["pattern"] for r in mine_patterns(conn, "demo", "0.1", minsup=2)]
    assert patterns
    assert all(all(name.startswith("rte_") for name in pattern) for pattern in patterns)


def test_macro_calls_need_explicit_relation_filter(conn):
    from libref.astminer.models import CodeSnippet

    code = (
        "#define rte_wrap(x) rte_eth_rx_burst(x, 0)\n"
        "void use(void) {\n"
        "  rte_init();\n"
        "  rte_tx_burst(0, 0, 0, 32);\n"
        "  rte_wrap(1);\n"
        "}\n"
    )
    snippet = CodeSnippet(sample_id="macro.c", language="c", code=code)
    ingest_snippets(
        conn,
        "demo",
        "0.1",
        [snippet],
        source_key="reference:macro",
        source_role="reference_project",
        target_filter={"prefixes": ["rte_"]},
    )
    rows = conn.execute(
        "SELECT callee_name, relation_kind, confidence FROM call_instances"
        " WHERE source_role='reference_project'"
    ).fetchall()
    macro = {name: (kind, conf) for name, kind, conf in rows if kind == "macro"}
    assert "rte_eth_rx_burst" in macro
    assert all(conf != "EXTRACTED" for _kind, conf in macro.values())
    default = mine_patterns(conn, "demo", "0.1", minsup=1, include_ambiguous=True)
    assert all("rte_wrap" not in pattern for pattern in [r["pattern"] for r in default])
    included = mine_patterns(
        conn,
        "demo",
        "0.1",
        minsup=1,
        include_ambiguous=True,
        relation_kinds=["direct", "macro"],
    )
    assert any("rte_wrap" in r["pattern"] for r in included)


def test_snippet_evidence_points_back_to_host(conn):
    from libref.astminer.models import CodeSnippet

    snippet = CodeSnippet(
        sample_id="page-1",
        language="python",
        code="demo.run()\n",
        origin_location="https://example.org/docs#quickstart",
        origin_start_line=12,
        origin_end_line=12,
        origin_start_byte=200,
        origin_end_byte=210,
    )
    ingest_snippets(
        conn,
        "demo",
        "0.1",
        [snippet],
        source_key="docs:page-1",
    )
    row = conn.execute(
        "SELECT caller_file, line_start, start_byte FROM call_instances"
        " WHERE caller_file LIKE 'https://example.org/docs%' LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "https://example.org/docs#quickstart"
    assert row[1] == 12
    assert row[2] == 200


def test_usage_source_never_defines_target_api(conn, tmp_path):
    repo = copy_mini_repo(tmp_path)
    index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos-reference",
        source_role="reference_project",
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM api_entities WHERE version_id="
            " (SELECT id FROM versions WHERE version='0.1' LIMIT 1)"
        ).fetchone()[0]
        == 0
    )
    index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos-implementation",
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM api_entities WHERE version_id="
            " (SELECT id FROM versions WHERE version='0.1' LIMIT 1)"
        ).fetchone()[0]
        > 0
    )


def test_unconstrained_snippet_is_not_target_usage(conn):
    snippet = CodeSnippet(sample_id="plain", language="python", code="demo.run()\n")
    ingest_snippets(conn, "demo", "0.1", [snippet], source_key="docs:plain")
    rows = conn.execute(
        "SELECT in_target_api FROM call_instances WHERE source_role='doc_snippet'"
    ).fetchall()
    assert rows
    assert all(int(row[0]) == 0 for row in rows)


def test_doc_pages_keep_independent_sources(conn):
    def _snippet(marker):
        return CodeSnippet(
            sample_id=f"page-{marker}",
            language="python",
            code=f"demo.run({marker})\n",
        )

    first = ingest_snippets(
        conn,
        "demo",
        "0.1",
        [_snippet(1)],
        source_key="doc:1:page-a",
        target_filter={"names": ["demo.run"]},
        source_url="https://example.org/a",
        selection={"url": "https://example.org/a", "heading": "A"},
    )
    second = ingest_snippets(
        conn,
        "demo",
        "0.1",
        [_snippet(2)],
        source_key="doc:1:page-b",
        target_filter={"names": ["demo.run"]},
        source_url="https://example.org/b",
        selection={"url": "https://example.org/b", "heading": "B"},
    )
    rows = conn.execute(
        "SELECT source_id FROM call_instances WHERE source_id IN (?,?)",
        (first["source_id"], second["source_id"]),
    ).fetchall()
    assert len(rows) == 2
    assert (
        conn.execute(
            "SELECT source_url FROM code_sources WHERE id=?", (second["source_id"],)
        ).fetchone()[0]
        == "https://example.org/b"
    )


def test_source_role_override_does_not_rewrite_default_profile(conn):
    from libref.services.code.usage_sources import register_source, resolve_profile
    from libref.services.registry.libraries import LibraryService

    _library_id, version_id = LibraryService(conn).ensure_library_version("demo", "0.2")
    official = register_source(
        conn,
        version_id=version_id,
        source_key="official",
        source_role="official_example",
    )
    tests = register_source(
        conn,
        version_id=version_id,
        source_key="tests",
        source_role="test",
    )
    default = resolve_profile(conn, version_id=version_id)
    assert default["source_ids"] == sorted([official, tests])
    subset = resolve_profile(conn, version_id=version_id, source_roles=["test"])
    assert subset["source_ids"] == [tests]
    assert subset["name"] != "usage"
    assert resolve_profile(conn, version_id=version_id)["source_ids"] == sorted(
        [official, tests]
    )


def test_profile_fingerprint_tracks_source_commit(conn):
    from libref.services.code.usage_sources import (
        ensure_profile,
        register_source,
        update_source_commit,
    )
    from libref.services.registry.libraries import LibraryService

    _library_id, version_id = LibraryService(conn).ensure_library_version("demo", "0.3")
    source = register_source(
        conn,
        version_id=version_id,
        source_key="reference:mtl",
        source_role="reference_project",
        source_url="https://github.com/example/mtl.git",
    )
    first = ensure_profile(
        conn, version_id=version_id, name="usage", source_ids=[source]
    )
    before = conn.execute(
        "SELECT fingerprint FROM mining_profiles WHERE id=?", (first,)
    ).fetchone()[0]
    update_source_commit(conn, source, "abc123")
    ensure_profile(conn, version_id=version_id, name="usage", source_ids=[source])
    after = conn.execute(
        "SELECT fingerprint FROM mining_profiles WHERE id=?", (first,)
    ).fetchone()[0]
    assert before != after


def test_sample_id_carries_source_namespace():
    from libref.astminer import resolution as ast_resolution
    from libref.astminer.models import RawCall

    call = RawCall(
        raw_callee="run",
        caller_file="main.py",
        caller="main",
        line_start=1,
        line_end=1,
    )
    first = ast_resolution.resolve_calls(
        [], {}, [call], "repo-a", sample_prefix="repo-a"
    )
    second = ast_resolution.resolve_calls(
        [], {}, [call], "repo-b", sample_prefix="repo-b"
    )
    assert first[0].sample_id != second[0].sample_id
