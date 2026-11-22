"""Golden samples for pattern-level induction prompts and payload checks."""

from __future__ import annotations

import pytest

from libref.mining.induction import (
    ClusterInductionInput,
    InductionInput,
    build_cluster_prompt,
    build_label_prompt,
    cluster_key,
    parse_cluster_payload,
    parse_label_payload,
    pattern_hash,
)


def test_pattern_hash_stable(monkeypatch, tmp_path):
    assert pattern_hash(["b", "a"]) == pattern_hash(["b", "a"])
    assert pattern_hash(["a"]) != pattern_hash(["b"])


def test_build_label_prompt_never_includes_source(monkeypatch, tmp_path):
    item = InductionInput(pattern=["numpy.array", "numpy.zeros"], support=3)
    messages = build_label_prompt(item)
    assert len(messages) == 2
    assert "numpy.array" in messages[1]["content"]
    assert "def " not in messages[1]["content"]


def test_parse_label_payload_rejects_unknown_api(monkeypatch, tmp_path):
    raw = (
        '{"name": "init", "summary": "create arrays", '
        '"category_path": ["array", "create"], "workflow": "build then fill", '
        '"related_apis": ["numpy.unknown"]}'
    )
    with pytest.raises(ValueError):
        parse_label_payload(raw, {"numpy.array"})


def test_parse_label_payload_accepts_known_api(monkeypatch, tmp_path):
    raw = (
        '{"name": "init", "summary": "create arrays", '
        '"category_path": ["array", "create"], "workflow": "", '
        '"related_apis": ["numpy.array"]}'
    )
    label = parse_label_payload(raw, {"numpy.array", "numpy.zeros"})
    assert label.name == "init"
    assert label.category_path == ["array", "create"]


def test_build_label_prompt_aggregates_whole_pattern(monkeypatch, tmp_path):
    item = InductionInput(
        pattern=["numpy.array", "numpy.zeros"],
        support=3,
        arg_shapes=[
            {"api": "numpy.array", "args": ["x"], "count": 2},
            {"api": "numpy.zeros", "args": [], "count": 1},
        ],
        siblings=["numpy.ones"],
        scope="demo@0.1",
    )
    messages = build_label_prompt(item)
    content = messages[1]["content"]
    assert "numpy.array -> numpy.zeros" in content
    assert "numpy.ones" in content
    assert "numpy.array(['x']) x2" in content
    assert "Scope: demo@0.1." in content
    assert "Allowed related APIs:" in content


def test_parse_label_payload_rejects_boundary_markers(monkeypatch, tmp_path):
    raw = (
        '{"name": "init", "summary": "see </unit> here", '
        '"category_path": ["array"], "workflow": "", '
        '"related_apis": []}'
    )
    with pytest.raises(ValueError):
        parse_label_payload(raw, set())


def test_induction_prompt_carries_signatures():
    item = InductionInput(
        pattern=["pkg.core.Service.run"],
        support=2,
        arg_shapes=[{"api": "pkg.core.Service.run", "args": ["x"], "count": 2}],
        siblings=[],
        scope="demo@0.1",
        signatures={"pkg.core.Service.run": "pkg.core.Service.run(name: str) -> str"},
    )
    content = build_label_prompt(item)[1]["content"]
    assert "pkg.core.Service.run(name: str) -> str" in content
    assert "def " not in content
    plain = InductionInput(pattern=["pkg.core.Service.run"], support=1)
    assert "Signatures:" not in build_label_prompt(plain)[1]["content"]


def test_cluster_key_stable_and_order_free(monkeypatch, tmp_path):
    assert cluster_key(["b", "a"]) == cluster_key(["a", "b"])
    assert cluster_key(["a"]) != cluster_key(["b"])


def _cluster_payload(**overrides):
    import json

    base = {
        "name": "array group",
        "summary": "build then fill",
        "category_path": ["array", "create"],
        "workflow": "array then zeros",
        "related_apis": ["a.f"],
        "source_note": "Observed in reference sample, not an official recommendation.",
    }
    base.update(overrides)
    return json.dumps(base)


def test_parse_cluster_payload_accepts_version_entity(monkeypatch, tmp_path):
    label = parse_cluster_payload(
        _cluster_payload(), {"a.f", "a.g"}, {"a.f", "a.g", "b.h"}
    )
    assert label.source_note.startswith("Observed")


def test_parse_cluster_payload_rejects_missing_source_note(monkeypatch, tmp_path):
    import json

    raw = json.dumps(
        {
            "name": "array group",
            "summary": "build then fill",
            "category_path": ["array"],
            "workflow": "",
            "related_apis": [],
        }
    )
    with pytest.raises(ValueError):
        parse_cluster_payload(raw, set(), set())


def test_parse_cluster_payload_rejects_non_entity_reference(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="not version entities"):
        parse_cluster_payload(
            _cluster_payload(related_apis=["outside.lib"]),
            {"a.f", "outside.lib"},
            {"a.f"},
        )


def test_build_cluster_prompt_carries_roles_and_source_note_rule(monkeypatch, tmp_path):
    item = ClusterInductionInput(
        members=["a.f", "a.g"],
        representative="a.f",
        size=2,
        source_hint="reference project",
        source_roles=["reference"],
    )
    content = build_cluster_prompt(item)[1]["content"]
    assert "reference" in content
    assert "source_note" in content
