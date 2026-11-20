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
