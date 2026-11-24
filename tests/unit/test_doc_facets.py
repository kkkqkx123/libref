"""Unit checks for deterministic document structure and content facets."""

from __future__ import annotations

from libref.chunking.doc_facets import (
    DOC_FACETS,
    FACET_BLOCK_TYPE,
    FACET_CODE_RATIO,
    FACET_DEPRECATED,
    DocBlock,
    aggregate_doc_facets,
    code_hash,
    fence_codes,
)


def test_fence_and_type_detection():
    block = DocBlock(
        document_id=1,
        content="# Client\n\n```python\nclient.connect()\n```\n\n| name | value |\n|---|---|\n| a | b |\n",
        path="Client / Examples",
    )
    result = aggregate_doc_facets([block], terms_per_facet=5)
    values = {row.facet: row.value for row in result.rows}
    assert values[FACET_BLOCK_TYPE]["kind"] == "example"
    assert values[FACET_CODE_RATIO]["details"]["value"] > 0
    assert values["has_table"]["details"]["value"] is True
    assert values["example_language"]["details"]["value"] == ["python"]


def test_deprecated_marker_and_budget():
    block = DocBlock(
        document_id=2,
        content="This API is deprecated and no longer supported in 1.2.3.",
        path="Notes",
    )
    result = aggregate_doc_facets(
        [block],
        enabled_facets=[FACET_DEPRECATED, "version_marker", "term_candidates"],
        terms_per_facet=2,
        candidate_budget=1,
    )
    assert result.dropped == 2
    assert len(result.rows) == 1
    assert result.rows[0].facet == FACET_DEPRECATED
    assert result.rows[0].value["details"]["value"] is True


def test_snippet_symbols_are_backfilled():
    block = DocBlock(document_id=3, content="See demo.Client.connect().")
    result = aggregate_doc_facets(
        [block],
        enabled_facets=["example_symbols"],
        snippet_symbols={3: ["demo.Client.connect", "demo.Client.connect"]},
    )
    assert result.rows[0].value["details"]["value"] == ["demo.Client.connect"]


def test_fence_codes_are_whitespace_insensitive():
    assert code_hash("a  b") == code_hash("a b")
    assert fence_codes("```python\nx = 1\n```\ntext\n```py\ny = 2\n```") == [
        "x = 1",
        "y = 2",
    ]
    assert set(DOC_FACETS) >= {FACET_BLOCK_TYPE, FACET_DEPRECATED}


def test_param_and_return_facets_are_structured() -> None:
    block = DocBlock(
        document_id=4,
        content=(
            "## Parameters\n\n"
            "| Parameter | Type |\n|---|---|\n| timeout | int |\n\n"
            "## Returns\n\n"
            "`Response` object\n"
        ),
        path="Client / Connect",
    )
    result = aggregate_doc_facets(
        [block], enabled_facets=["param_names", "return_hint"]
    )
    values = {row.facet: row.value["details"]["value"] for row in result.rows}
    assert values["param_names"]["names"] == ["timeout"]
    assert values["param_names"]["source"] == "table"
    assert values["return_hint"] == {
        "present": True,
        "type": "Response",
        "source": "heading",
    }


def test_return_hint_skips_empty_markdown_cells() -> None:
    block = DocBlock(
        document_id=6,
        content="## Returns\n\n|---|---|\n| Response | object |\n",
        path="Client",
    )
    result = aggregate_doc_facets([block], enabled_facets=["return_hint"])
    values = {row.facet: row.value["details"]["value"] for row in result.rows}
    assert values["return_hint"] == {
        "present": True,
        "type": "Response",
        "source": "heading",
    }


def test_signature_facts_override_prose_keywords() -> None:
    block = DocBlock(
        document_id=5,
        content="Connect a client with options and settings.\n",
        path="Client",
    )
    result = aggregate_doc_facets(
        [block],
        enabled_facets=["param_names", "return_hint"],
        snippet_symbols={5: ["demo.Client.connect"]},
        entity_signatures={
            "demo.Client.connect": {
                "qualified_name": "demo.Client.connect",
                "params": ["timeout", "retries"],
                "return_type": "Response",
                "signature": "",
            }
        },
    )
    values = {row.facet: row.value["details"]["value"] for row in result.rows}
    assert values["param_names"]["names"] == ["timeout", "retries"]
    assert values["param_names"]["source"] == "signature"
    assert values["return_hint"]["source"] == "signature"
