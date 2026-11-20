"""Exit-level usage-mining review output: deterministic fixtures drive every report."""

from __future__ import annotations

import json

import pytest

from libref.db import apply_migrations, open_db
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

_REPORTS = (
    "01-sources.txt",
    "02-profiles.txt",
    "03-patterns-implementation.txt",
    "04-patterns-usage.txt",
    "05-patterns-usage-official.txt",
    "06-patterns-usage-reference.txt",
    "10-evidence-usage.txt",
    "11-evidence-usage-rendered.txt",
    "12-health-usage.txt",
    "13-labels-usage.txt",
    "14-differential.txt",
)


def test_usage_mining_outputs_are_reviewable(tmp_path, monkeypatch) -> None:
    from tests.e2e.render_usage_mining_outputs import load_usage_cases, render

    cases = load_usage_cases()
    db_path = tmp_path / "usage-mining-review.db"
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=str(db_path),
        discovery_offline=True,
        registry_offline=True,
        induction_enabled=True,
        simvec_dir=str(tmp_path / "simvec"),
    )
    out_dir = tmp_path / "outputs" / "usage_mining"
    try:
        summary = render(conn, settings, out_dir)
    finally:
        conn.close()

    assert all(summary["checks"].values()), summary["checks"]
    assert summary["sources"] == len(cases["usage_sources"]) + 1
    assert summary["profiles"] == len(cases["profiles"]) + 1
    assert summary["labels"] >= 1
    for name in _REPORTS:
        assert (out_dir / name).is_file(), name

    profiles = json.loads((out_dir / "02-profiles.txt").read_text(encoding="utf-8"))
    stored_names = {item["name"] for item in profiles["stored"]}
    assert stored_names == {
        "implementation",
        *(str(item["name"]) for item in cases["profiles"]),
    }

    differential = json.loads(
        (out_dir / "14-differential.txt").read_text(encoding="utf-8")
    )
    assert all(differential["checks"].values())
    for api in cases["expect"]["implementation_only"]:
        assert api in differential["implementation"]
    for api in cases["expect"]["usage_excludes"]:
        assert api not in differential["usage"]

    evidence = (out_dir / "11-evidence-usage-rendered.txt").read_text(encoding="utf-8")
    assert 'source="' in evidence
    assert 'sample="' in evidence
    assert 'profile_fingerprint="' in evidence
    assert 'target="resolved_target"' in evidence

    health = json.loads((out_dir / "12-health-usage.txt").read_text(encoding="utf-8"))
    assert "macro_coverage" in health
    assert "relations" in health
    assert health["target_status"]
    assert "prefix_candidate_rate" in health

    labels = json.loads((out_dir / "13-labels-usage.txt").read_text(encoding="utf-8"))
    assert labels["labels"]
    assert all(item["profile"] == "usage" for item in labels["labels"])
    assert 'profile_fingerprint="' in labels["answer_block"]
    assert 'source_hint="' in labels["answer_block"]
