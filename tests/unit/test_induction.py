"""Golden samples for pattern-level induction prompts and payload checks."""

from __future__ import annotations

import pytest

from libref.mining.induction import (
    InductionInput,
    build_label_prompt,
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
