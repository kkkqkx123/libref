"""Contract checks for the handwritten document-feature judgment fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "e2e" / "fixtures" / "doc_features"
DEPRECATION = re.compile(
    r"deprecated|deprecation|removed|no longer|弃用|移除|不再", re.IGNORECASE
)

pytestmark = pytest.mark.unit


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_frozen_questions_follow_authoring_rules() -> None:
    questions = _load("questions.json")
    assert set(questions) == {
        "demo",
        "httpx/docs",
        "zod/docs",
        "jsoup/docs",
        "fiber/docs",
        "vue/docs",
        "pydantic/docs",
        "FluentValidation/docs",
        "Rocket/docs",
        "gradle/docs",
    }
    assert "demo" not in questions or len(questions["demo"]) == 12

    frozen = {name: rows for name, rows in questions.items() if name != "demo"}
    assert len(frozen) == 9
    total = 0
    for name, rows in frozen.items():
        assert len(rows) == 30, name
        splits = {"dev": 0, "test": 0}
        urls: dict[str, str] = {}
        for row in rows:
            total += 1
            for key in (
                "id",
                "source",
                "source_ref",
                "type",
                "query_lang",
                "query",
                "targets",
                "expected_facets",
                "grades",
            ):
                assert row.get(key), (name, row.get("id"), key)
            assert set(row["grades"]) == {"strong", "general", "miss"}
            assert row["expected_facets"].get("heading")
            assert row["expected_facets"].get("block_type")
            assert row["split"] in splits
            splits[row["split"]] += 1
            for url in row["targets"]:
                assert urls.setdefault(str(url), row["split"]) == row["split"], (
                    name,
                    url,
                )
            query = str(row["query"])
            for entity in row.get("expected_entities") or []:
                assert entity in query or entity.split(".")[-1] in query, (
                    name,
                    row["id"],
                    entity,
                )
            verdict = row.get("expected_verdict")
            if verdict:
                assert verdict == "drift", (name, row["id"], verdict)
                assert DEPRECATION.search(query), (name, row["id"])
            if "expected_feature_nodes" in row:
                assert row["expected_feature_nodes"], (name, row["id"])
        assert splits == {"dev": 15, "test": 15}, name
    assert total == 270


def test_drift_samples_have_manual_verdicts() -> None:
    samples = _load("drift_samples.json")["samples"]
    assert len(samples) == 40
    counts = {"drift": 0, "not_drift": 0}
    for sample in samples:
        for key in (
            "corpus",
            "url",
            "heading",
            "auto_verdict",
            "human_verdict",
            "reason",
        ):
            assert sample.get(key), (sample.get("corpus"), key)
        assert sample["auto_verdict"] in {"drift", "match", "unknown"}
        human = sample["human_verdict"]
        assert human in counts, (sample.get("corpus"), human)
        counts[human] += 1
        if human == "drift":
            assert sample["reason"].strip()
    assert counts == {"drift": 28, "not_drift": 12}
