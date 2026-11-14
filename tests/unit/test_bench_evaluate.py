"""Judgment contract and pool diagnosis, the two pieces the waves build on.

Global scope lets a judgment ask which library answers, not which page; the
language pair decides which side of the cross-lingual comparison a row belongs
to; the pool buckets decide whether a different aggregation could have helped.
"""

from __future__ import annotations

import json

import pytest

from tests.e2e.bench.evaluate import _classify_pool, _load

pytestmark = pytest.mark.unit


def _judgments_file(path, entries):
    target = path / "judgments.json"
    target.write_text(json.dumps(entries), encoding="utf-8")
    return target


def _entry(**overrides):
    base = {
        "id": "Q1",
        "type": "qualified",
        "split": "dev" if overrides.pop("dev", False) else "test",
        "query": "timeout settings",
        "targets": [],
    }
    base.update(overrides)
    return base


def test_load_derives_query_language_from_script(tmp_path):
    path = _judgments_file(
        tmp_path,
        {
            "corpus": [
                _entry(id="Q1", dev=True, query="证书校验"),
                _entry(id="Q2", query="timeout settings"),
            ]
        },
    )
    data = _load(path)
    assert [entry["query_lang"] for entry in data["corpus"]] == ["zh", "en"]


def test_load_rejects_unknown_scope_and_language(tmp_path):
    path = _judgments_file(tmp_path, {"corpus": [_entry(scope="everywhere")]})
    with pytest.raises(SystemExit):
        _load(path)
    path = _judgments_file(tmp_path, {"corpus": [_entry(query_lang="fr")]})
    with pytest.raises(SystemExit):
        _load(path)


def test_pool_buckets_separate_aggregation_from_recall():
    assert _classify_pool(2, True) == "kept"
    assert _classify_pool(0, True) == "aggregation_loss"
    assert _classify_pool(0, False) == "recall_loss"
