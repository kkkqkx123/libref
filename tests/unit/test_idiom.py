"""Golden samples for readable idiom assembly, evidence dedup, and health signals."""

from __future__ import annotations

from libref.mining.idiom import (
    deduplicate_evidence,
    mask_arg,
    normalize_signature,
    reuse_diversity,
    synthesize_snippet,
)
from libref.mining.scoring import capped_alpha, pattern_contains_all


def test_mask_arg_keyword_shape():
    assert mask_arg("path='a.py'") == "path='...'"
    assert mask_arg("count=3") == "count=0"
    assert mask_arg("verbose=True") == "verbose=True"
    assert mask_arg("") == "_"
    assert mask_arg("data") == "_"


def test_synthesize_snippet_keeps_order_and_holes():
    calls = [
        {
            "callee": "a",
            "args": ["x"],
            "line_start": 1,
            "line_end": 1,
            "confidence": "EXTRACTED",
        },
        {
            "callee": "skip",
            "args": [],
            "line_start": 2,
            "line_end": 2,
            "confidence": "EXTRACTED",
        },
        {
            "callee": "b",
            "args": ["y=1"],
            "line_start": 3,
            "line_end": 3,
            "confidence": "INFERRED",
        },
    ]
    out = synthesize_snippet(["a", "b"], calls, [0, 2])
    assert not out["invalid"]
    assert out["skipped"] == 1
    texts = [line.get("text") for line in out["lines"] if not line.get("gap")]
    assert texts[0].startswith("a(")
    assert texts[-1].startswith("b(")
    assert any(line.get("gap") for line in out["lines"])


def test_deduplicate_keeps_one_per_file_and_merges_signatures():
    def _group(path, caller):
        calls = [
            {
                "callee": "numpy.array",
                "args": ["x"],
                "line_start": 1,
                "line_end": 1,
                "confidence": "EXTRACTED",
            },
            {
                "callee": "numpy.zeros",
                "args": ["3"],
                "line_start": 2,
                "line_end": 2,
                "confidence": "EXTRACTED",
            },
        ]
        return {
            "caller_file": path,
            "caller": caller,
            "calls": calls,
            "matched": [0, 1],
        }

    groups = [_group("f.py", "g1"), _group("f.py", "g2"), _group("h.py", "g1")]
    out = deduplicate_evidence(groups, per_file_limit=1, cross_file_dedup=True)
    assert len(out) == 1
    assert out[0]["caller_file"] == "f.py"
    kept = deduplicate_evidence(groups, per_file_limit=2, cross_file_dedup=False)
    assert len(kept) == 3


def test_normalize_signature_stable():
    calls = [
        {"callee": "a.b", "args": ["x", "y"]},
        {"callee": "c", "args": []},
    ]
    assert normalize_signature(calls, [0, 1]) == "a.b(_,_)|c()"


def test_reuse_diversity_logs():
    health = reuse_diversity(
        [{"support": 4}, {"support": 2}], [{"members": {"a"}}, {"members": {"b"}}]
    )
    assert health["patterns"] == 2
    assert health["total_support"] == 6
    assert health["reusability"] > 0
    assert health["clusters"] == 2
    single = reuse_diversity([{"support": 5}], [{"members": {"a"}}])
    assert single["reusability"] == 0.0


def test_pattern_contains_all_and_capped_alpha():
    assert pattern_contains_all(["a", "b"], ["a"])
    assert not pattern_contains_all(["a"], ["a", "b"])
    assert pattern_contains_all(["a"], [])
    assert capped_alpha(0.15, 0.0) <= 0.02
    assert capped_alpha(0.15, 2.0) == 0.15
    assert capped_alpha(0.0, 2.0) == 0.0
