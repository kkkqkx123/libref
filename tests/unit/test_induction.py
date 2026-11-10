"""Golden samples for pattern-level induction prompts and payload checks."""

from __future__ import annotations

import pytest

from libref.mining.induction import (
    InductionInput,
    build_label_prompt,
    build_taxonomy_prompt,
    merge_taxonomy,
    parse_label_payload,
    pattern_hash,
)


def test_pattern_hash_stable():
    assert pattern_hash(["b", "a"]) == pattern_hash(["b", "a"])
    assert pattern_hash(["a"]) != pattern_hash(["b"])


def test_build_label_prompt_never_includes_source():
    item = InductionInput(pattern=["numpy.array", "numpy.zeros"], support=3)
    messages = build_label_prompt(item)
    assert len(messages) == 2
    assert "numpy.array" in messages[1]["content"]
    assert "def " not in messages[1]["content"]


def test_parse_label_payload_rejects_unknown_api():
    raw = (
        '{"name": "init", "summary": "create arrays", '
        '"category_path": ["array", "create"], "workflow": "build then fill", '
        '"related_apis": ["numpy.unknown"]}'
    )
    with pytest.raises(ValueError):
        parse_label_payload(raw, {"numpy.array"})


def test_parse_label_payload_accepts_known_api():
    raw = (
        '{"name": "init", "summary": "create arrays", '
        '"category_path": ["array", "create"], "workflow": "", '
        '"related_apis": ["numpy.array"]}'
    )
    label = parse_label_payload(raw, {"numpy.array", "numpy.zeros"})
    assert label.name == "init"
    assert label.category_path == ["array", "create"]


def test_merge_taxonomy_deterministic():
    first = {"array": {"create": ["numpy.array"]}}
    second = {"array": {"fill": ["numpy.zeros"]}}
    merged = merge_taxonomy([second, first])
    assert merged["array"]["create"]["__apis__"] == ["numpy.array"]
    assert merged["array"]["fill"]["__apis__"] == ["numpy.zeros"]
    assert merge_taxonomy([first, second]) == merged


def test_build_taxonomy_prompt_lists_names():
    messages = build_taxonomy_prompt(["numpy.array", "numpy.zeros"])
    assert "numpy.array" in messages[1]["content"]
    assert "numpy.zeros" in messages[1]["content"]
