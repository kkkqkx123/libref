"""Real repository usage mining: pinned commits, production services, review outputs.

Skipped by default; requires LIBREF_RUN_E2E=1 and a completed sync via
tests/e2e/sync_usage_repos.py. A missing checkout marks the environment
unavailable rather than a regression.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from libref.astminer.models import (
    TARGET_STATUS_PREFIX,
    TARGET_STATUS_RESOLVED,
    TARGET_STATUS_UNCONFIRMED,
)
from tests.conftest import make_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("LIBREF_RUN_E2E") != "1",
    reason="e2e requires LIBREF_RUN_E2E=1 and pinned real repositories",
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LOCK = HERE / "fixtures" / "usage_mining" / "repos.lock.json"

_REPORTS = (
    "00-manifest.txt",
    "01-sources.txt",
    "02-profiles.txt",
    "03-patterns-implementation.txt",
    "04-patterns-dpdk-official-examples.txt",
    "05-patterns-dpdk-on-mtl.txt",
    "06-patterns-usage.txt",
    "07-diff-official-vs-mtl.txt",
    "08-version-compatibility.txt",
    "09-resolution-coverage-diff.txt",
    "10-evidence-dpdk-official-examples.txt",
    "11-evidence-dpdk-on-mtl.txt",
    "20-health-dpdk-official-examples.txt",
    "21-health-dpdk-on-mtl.txt",
    "30-labels-dpdk-official-examples.txt",
    "31-labels-dpdk-on-mtl.txt",
    "40-health-comparison.txt",
    "50-quality-gates.txt",
    "SUMMARY.txt",
)


def _read(out_dir: Path, name: str):
    return json.loads((out_dir / name).read_text(encoding="utf-8"))


def test_real_repo_usage_mining_outputs(tmp_path, conn, monkeypatch) -> None:
    from tests.e2e.run_usage_mining_real import render

    if not LOCK.is_file():
        pytest.skip("run tests/e2e/sync_usage_repos.py first")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    for repo in lock["repos"].values():
        if not (ROOT / repo["path"]).is_dir():
            pytest.skip(f"pinned repo unavailable: {repo['path']}")

    db_path = tmp_path / "usage-real.db"
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=str(db_path),
        discovery_offline=True,
        registry_offline=True,
        induction_enabled=True,
        simvec_dir=str(tmp_path / "simvec"),
    )
    out_dir = tmp_path / "outputs" / "usage_mining_real"
    summary = render(conn, settings, out_dir, repos_dir=tmp_path / "indexed-repos")

    assert all(summary["checks"].values()), summary["checks"]
    for name in _REPORTS:
        assert (out_dir / name).is_file(), name

    sources = _read(out_dir, "01-sources.txt")
    assert {item["source_role"] for item in sources} == {
        "implementation",
        "official_example",
        "reference_project",
    }
    commits = {item["commit_sha"] for item in sources}
    expected = {item["commit"] for item in lock["repos"].values()}
    assert commits >= expected
    assert all(item["source_url"].startswith("https://") for item in sources)

    profiles = _read(out_dir, "02-profiles.txt")
    by_name = {item["name"]: item for item in profiles}
    assert by_name["usage"]["source_ids"] == [
        by_name["dpdk-official-examples"]["source_ids"][0],
        by_name["dpdk-on-mtl"]["source_ids"][0],
    ]
    assert (
        by_name["implementation"]["source_ids"][0] not in by_name["usage"]["source_ids"]
    )

    for name in (
        "04-patterns-dpdk-official-examples.txt",
        "05-patterns-dpdk-on-mtl.txt",
    ):
        patterns = _read(out_dir, name)
        assert patterns
        assert all(
            api.lower().startswith("rte_")
            for item in patterns
            for api in item["pattern"]
        )

    diff = _read(out_dir, "07-diff-official-vs-mtl.txt")
    assert {"official_only", "mtl_only", "shared"} <= set(diff)
    assert diff["official_only"] and diff["mtl_only"]
    assert diff["implementation_pollutes_usage"] is False

    version_report = _read(out_dir, "08-version-compatibility.txt")
    reference = next(
        item for item in version_report if item["source_role"] == "reference_project"
    )
    assert reference["declared_library_version"] == "23.07"
    assert reference["target_status"] == "version_unconfirmed"
    assert "CHANGELOG.md" in reference["evidence"]

    resolution = _read(out_dir, "09-resolution-coverage-diff.txt")
    assert {"unresolved_rate", "prefix_candidate_rate"} <= set(
        resolution["metric_delta"]
    )
    assert resolution["improved"] is True
    assert resolution["macro_calls_delta"] > 0
    assert set(resolution["target_calls_delta"]) == {
        "dpdk-official-examples",
        "dpdk-on-mtl",
    }
    assert (
        resolution["variants"]["baseline"]["index"]["implementation"]["counts"]["files"]
        == 2
    )

    expected_status = {
        "dpdk-official-examples": {TARGET_STATUS_RESOLVED, TARGET_STATUS_PREFIX},
        "dpdk-on-mtl": {TARGET_STATUS_UNCONFIRMED},
    }
    for idx, (profile, role) in enumerate(
        (
            ("dpdk-official-examples", "official_example"),
            ("dpdk-on-mtl", "reference_project"),
        ),
        start=10,
    ):
        evidence = _read(out_dir, f"{idx:02d}-evidence-{profile}.txt")
        assert evidence
        assert all(item["source_role"] == role for item in evidence)
        assert all(
            item["profile_fingerprint"]
            and item["sample_id"]
            and item["canonical_url"].startswith("https://")
            and "#L" in item["canonical_url"]
            for item in evidence
        )
        statuses = {
            status for item in evidence for status in item["target_status_counts"]
        }
        assert statuses & expected_status[profile]

    health_official = _read(out_dir, "20-health-dpdk-official-examples.txt")
    health_mtl = _read(out_dir, "21-health-dpdk-on-mtl.txt")
    assert health_official["confidence"] != health_mtl["confidence"]
    assert health_official["unresolved_rate"] != health_mtl["unresolved_rate"]
    assert health_official["target_status"]
    assert set(health_official["target_status"]) <= {
        "resolved_target",
        "prefix_candidate",
        "version_unconfirmed",
    }

    comparison = _read(out_dir, "40-health-comparison.txt")
    assert comparison["dpdk-official-examples"]["macro_coverage"]["total"] > 0
    assert comparison["dpdk-on-mtl"]["macro_coverage"]["total"] > 0

    labels = _read(out_dir, "31-labels-dpdk-on-mtl.txt")
    assert labels["labels"]
    assert all(item["profile"] == "dpdk-on-mtl" for item in labels["labels"])
    assert "unofficial" in labels["source_hint"]
    assert "mtl" in labels["source_hint"]
    assert "v23.12" in labels["source_hint"]
    assert "f20b60a982e2c38e61108c4123ee7cb5a385da16" in labels["source_hint"]
    assert 'source_hint="' in labels["answer_block"]

    gates = _read(out_dir, "50-quality-gates.txt")
    assert all(gates["checks"].values()), gates["gates"]
    assert all(gate["observed"] for gate in gates["gates"])
    partition = next(
        gate for gate in gates["gates"] if gate["name"] == "full_corpus_partition"
    )
    assert partition["observed"]["repo_totals"]["target"] > 1000
    assert "drivers" in partition["observed"]["uncovered_dirs"]["target"]
    assert set(gates["thresholds"]) == {
        "unresolved_rate",
        "prefix_candidate_rate",
        "minimum_macro_ratio",
    }

    summary_text = _read(out_dir, "SUMMARY.txt")
    assert summary_text["corpus"]["scope"] == (
        "complete source-file partition of pinned repositories by role"
    )
    files = summary_text["corpus"]["files"]
    totals = summary_text["corpus"]["repo_totals"]
    assert (
        files["implementation"]["files"] + files["official_example"]["files"]
        == totals["target"]
    )
    assert files["reference_project"]["files"] == totals["reference"]
    assert all(not item["missing_files"] for item in files.values())
    assert "drivers" in summary_text["corpus"]["uncovered_dirs"]["target"]
    assert summary_text["needs_review"]
