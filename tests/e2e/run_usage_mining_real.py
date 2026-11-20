"""Real-repo usage mining runner with source-aware review exports.

Usage: uv run python tests/e2e/run_usage_mining_real.py [--out-dir DIR]
Reads repos.lock.json produced by sync_usage_repos.py, indexes the pinned local
checkouts through production services, mines the configured profiles, and writes
reports to tests/e2e/outputs/usage_mining_real. No network access here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from libref.astminer.models import (
    TARGET_STATUS_PREFIX,
    TARGET_STATUS_RESOLVED,
    TARGET_STATUS_UNCONFIRMED,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MANIFEST = HERE / "fixtures" / "usage_mining" / "repos.json"
LOCK = HERE / "fixtures" / "usage_mining" / "repos.lock.json"
DEFAULT_OUT = HERE / "outputs" / "usage_mining_real"
PROFILES = {
    "dpdk-official-examples": "official_example",
    "dpdk-on-mtl": "reference_project",
}
ROLE_OF_PROFILE = {"implementation": "implementation", "usage": "*", **PROFILES}
MINE_PARAMS = {"minsup": 3, "minlen": 2, "maxlen": 8, "topn": 50}
MAX_REPORT_PATTERNS = 50
REVIEW_THRESHOLDS = {
    "unresolved_rate": 0.75,
    "prefix_candidate_rate": 0.60,
    "minimum_macro_ratio": 0.02,
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run real-repo usage mining review")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _write(out_dir: Path, name: str, body: str) -> None:
    (out_dir / name).write_text(body.rstrip() + "\n", encoding="utf-8")


def _inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    if not LOCK.is_file():
        raise FileNotFoundError(
            f"missing {LOCK}; run tests/e2e/sync_usage_repos.py first"
        )
    return (
        json.loads(MANIFEST.read_text(encoding="utf-8")),
        json.loads(LOCK.read_text(encoding="utf-8")),
    )


async def _label_chat(messages: list[dict[str, Any]]) -> str:
    user = next(
        (str(item["content"]) for item in messages if item.get("role") == "user"), ""
    )
    if "Source context:" not in user:
        raise AssertionError("label prompt is missing source provenance")
    return json.dumps(
        {
            "name": "real usage pattern",
            "summary": "deterministic review label for real repositories",
            "category_path": ["usage", "real-repo"],
            "workflow": "",
            "related_apis": [],
        },
        ensure_ascii=False,
    )


def _source_rows(conn, version_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, source_key, source_role, origin_kind, source_url, commit_sha,"
        " selection, target_filter FROM code_sources WHERE version_id=? ORDER BY id",
        (version_id,),
    ).fetchall()
    return [
        {
            "source_id": int(row[0]),
            "source_key": str(row[1]),
            "source_role": str(row[2]),
            "origin_kind": str(row[3]),
            "source_url": str(row[4] or ""),
            "commit_sha": str(row[5] or ""),
            "selection": json.loads(str(row[6])) if row[6] else {},
            "target_filter": json.loads(str(row[7])) if row[7] else {},
        }
        for row in rows
    ]


def _profile_rows(conn, version_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, source_ids, support_mode, fingerprint, params FROM mining_profiles"
        " WHERE version_id=? ORDER BY name",
        (version_id,),
    ).fetchall()
    return [
        {
            "name": str(row[0]),
            "source_ids": json.loads(str(row[1])) if row[1] else [],
            "support_mode": str(row[2]),
            "fingerprint": str(row[3] or ""),
            "params": json.loads(str(row[4])) if row[4] else {},
        }
        for row in rows
    ]


def _names(patterns: list[dict[str, Any]]) -> set[str]:
    return {str(name) for item in patterns for name in item.get("pattern", [])}


def _target_filter(manifest: dict[str, Any], corpus: dict[str, Any]) -> dict[str, Any]:
    filt = dict(corpus.get("target_filter") or {})
    repo = manifest[str(corpus["repo"])]
    filt.setdefault("declared_library_version", str(repo["declared_target_version"]))
    filt.setdefault("version_evidence", str(repo.get("version_evidence") or ""))
    filt.setdefault("repository", str(repo["name"]))
    filt.setdefault("target_tag", str(repo["tag"]))
    return filt


def _index_corpus(
    conn,
    manifest: dict[str, Any],
    lock: dict[str, Any],
    name: str,
    corpus: dict[str, Any],
    *,
    library: str,
    version: str,
    repos_dir: Path,
    base: Path,
    settings: Any,
    only_files: list[str] | None = None,
) -> dict[str, Any]:
    from libref.services.code.code_ingest import index_repo
    from libref.services.code.usage_sources import update_source_url

    repo = lock["repos"][str(corpus["repo"])]
    root = base / repo["path"]
    if not root.is_dir():
        raise FileNotFoundError(f"pinned repo missing: {root}")
    files = list(corpus.get("only_files") or []) if only_files is None else only_files
    result = index_repo(
        conn,
        library,
        version,
        str(root),
        repos_dir=repos_dir,
        ecosystem="custom",
        resolved_from="e2e-pinned",
        fresh=True,
        source_role=str(corpus["role"]),
        target_filter=_target_filter(manifest, corpus),
        subdir=corpus.get("subdir"),
        exclude_patterns=list(corpus.get("exclude_patterns") or []),
        max_file_bytes=int(corpus.get("max_file_bytes", 1_000_000)),
        only_files=files or None,
        settings=settings,
    )
    source_id = int(result["source_id"])
    update_source_url(conn, source_id, str(repo["url"]))
    conn.commit()
    return {
        "counts": result,
        "commit": repo["commit"],
        "path": repo["path"],
        "requested_files": files,
        "selection": {
            "subdir": corpus.get("subdir"),
            "exclude_patterns": list(corpus.get("exclude_patterns") or []),
            "max_file_bytes": int(corpus.get("max_file_bytes", 1_000_000)),
        },
        "missing_files": [name for name in files if not (root / name).is_file()],
    }


def _index_version(
    conn,
    settings: Any,
    manifest: dict[str, Any],
    lock: dict[str, Any],
    *,
    library: str,
    version: str,
    repos_dir: Path,
    only_files: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for name, corpus in manifest["corpora"].items():
        wanted = None
        if only_files and name in only_files:
            wanted = only_files[name]
        counts[name] = _index_corpus(
            conn,
            manifest,
            lock,
            name,
            corpus,
            library=library,
            version=version,
            repos_dir=repos_dir,
            base=ROOT,
            settings=settings,
            only_files=wanted,
        )
    return counts


def _call_facts(conn, version_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT source_id, source_role, in_target_api, target_status, resolve_kind,"
        " confidence, count(*) FROM call_instances WHERE version_id=?"
        " GROUP BY source_id, source_role, in_target_api, target_status,"
        " resolve_kind, confidence ORDER BY source_id, target_status",
        (version_id,),
    ).fetchall()
    return [
        {
            "source_id": int(row[0]),
            "source_role": str(row[1]),
            "in_target_api": int(row[2]),
            "target_status": str(row[3]),
            "resolve_kind": str(row[4]),
            "confidence": str(row[5]),
            "calls": int(row[6]),
        }
        for row in rows
    ]


def _profile_facts(facts: list[dict[str, Any]], source_ids: set[int]) -> dict[str, Any]:
    allowed = [row for row in facts if row["source_id"] in source_ids]
    target = [row for row in allowed if row["in_target_api"] == 1]
    statuses: dict[str, int] = {}
    mined_status: dict[str, int] = {}
    for row in target:
        count = int(row["calls"])
        statuses[row["target_status"]] = statuses.get(row["target_status"], 0) + count
        if row["target_status"] in (TARGET_STATUS_PREFIX, TARGET_STATUS_UNCONFIRMED):
            continue
        if row["confidence"] == "AMBIGUOUS" or row["resolve_kind"] in (
            "stdlib",
            "builtin",
        ):
            continue
        mined_status[row["target_status"]] = (
            mined_status.get(row["target_status"], 0) + count
        )
    return {
        "calls": sum(int(row["calls"]) for row in allowed),
        "target_calls": sum(int(row["calls"]) for row in target),
        "roles": sorted({row["source_role"] for row in allowed}),
        "target_status": statuses,
        "mined_status": mined_status,
        "unconfirmed_calls": statuses.get(TARGET_STATUS_UNCONFIRMED, 0),
        "resolved_calls": statuses.get(TARGET_STATUS_RESOLVED, 0),
        "prefix_calls": statuses.get(TARGET_STATUS_PREFIX, 0),
    }


def _version_report(
    source_rows: list[dict[str, Any]], version: str
) -> list[dict[str, Any]]:
    from libref.services.code.usage_sources import source_target_status

    out = []
    for source in source_rows:
        filt = source["target_filter"]
        declared = str(filt.get("declared_library_version") or "")
        status = source_target_status(
            source["source_role"],
            declared,
            version,
            version_required=bool(filt.get("version_required")),
        )
        out.append(
            {
                "source_key": source["source_key"],
                "source_role": source["source_role"],
                "target_version": version,
                "declared_library_version": declared,
                "target_status": status or "version_confirmed",
                "evidence": str(filt.get("version_evidence") or ""),
            }
        )
    return out


def _corpus_census(manifest: dict[str, Any], lock: dict[str, Any]) -> dict[str, Any]:
    """Count every source file covered by each role and by each repository."""
    from libref.astminer.config import all_suffixes
    from libref.astminer.repos import describe_source_files

    limits = {
        name: (repo["path"], int(corpus.get("max_file_bytes", 1_000_000)))
        for name, corpus in manifest["corpora"].items()
        for repo in [lock["repos"][str(corpus["repo"])]]
    }
    expected: dict[str, int] = {}
    for name, corpus in manifest["corpora"].items():
        rel, limit = limits[name]
        census = describe_source_files(
            ROOT / rel,
            all_suffixes(),
            subdir=corpus.get("subdir"),
            exclude_patterns=list(corpus.get("exclude_patterns") or []),
            max_file_bytes=limit,
        )
        expected[name] = len(census["files"])
    totals = {
        "target": expected["implementation"] + expected["official_example"],
        "reference": expected["reference_project"],
    }
    uncovered: dict[str, list[str]] = {}
    for key in ("target", "reference"):
        repo = lock["repos"][key]
        limit = max(
            int(corpus.get("max_file_bytes", 1_000_000))
            for corpus in manifest["corpora"].values()
            if str(corpus["repo"]) == key
        )
        full = describe_source_files(
            ROOT / repo["path"], all_suffixes(), max_file_bytes=limit
        )
        covered = {
            rel
            for name, corpus in manifest["corpora"].items()
            if str(corpus["repo"]) == key
            for rel in describe_source_files(
                ROOT / repo["path"],
                all_suffixes(),
                subdir=corpus.get("subdir"),
                exclude_patterns=list(corpus.get("exclude_patterns") or []),
                max_file_bytes=limit,
            )["files"]
            if name
        }
        uncovered[key] = sorted(
            {rel.split("/", 1)[0] for rel in full["files"] if rel not in covered}
        )
    return {
        "expected": expected,
        "repo_totals": totals,
        "uncovered_dirs": uncovered,
    }


def _macro_calls(conn, version_id: int) -> int:
    return int(
        conn.execute(
            "SELECT count(*) FROM call_instances WHERE version_id=?"
            " AND relation_kind='macro'",
            (version_id,),
        ).fetchone()[0]
    )


def _resolution_diff(
    conn,
    settings: Any,
    manifest: dict[str, Any],
    lock: dict[str, Any],
    repos_dir: Path,
    version: str,
) -> dict[str, Any]:
    """Compare the original header slice with the expanded declaration slice."""
    from libref.services.code.mining_read import MiningReadService
    from libref.services.code.mining_refresh import mine_patterns
    from libref.services.code.usage_sources import resolve_profile
    from libref.services.registry.libraries import LibraryService

    baseline_only = list(manifest["corpora"]["implementation"]["baseline_only_files"])
    variants: dict[str, Any] = {}
    for label, library, only_files in (
        ("baseline", "dpdk-e2e-baseline", baseline_only),
        ("enhanced", "dpdk-e2e", None),
    ):
        if label == "enhanced":
            counts = None
            _library_id, version_id = LibraryService(conn).ensure_library_version(
                library, version
            )
        else:
            counts = _index_version(
                conn,
                settings,
                manifest,
                lock,
                library=library,
                version=version,
                repos_dir=repos_dir / label,
                only_files={
                    name: [str(path) for path in item["baseline_only_files"]]
                    for name, item in manifest["corpora"].items()
                },
            )
            _library_id, version_id = LibraryService(conn).ensure_library_version(
                library, version
            )
        reader = MiningReadService(conn, settings)
        patterns = {
            name: _names(
                mine_patterns(
                    conn,
                    library,
                    version,
                    **MINE_PARAMS,
                    include_ambiguous=True,
                    include_unconfirmed=True,
                    profile=name,
                    source_roles=[role],
                )[:MAX_REPORT_PATTERNS]
            )
            for name, role in PROFILES.items()
        }
        resolve_profile(conn, version_id=version_id, name="implementation")
        health = {
            name: reader.mining_health(library, version, profile=name)
            for name in PROFILES
        }
        unresolved: list[dict[str, Any]] = []
        for name, role in PROFILES.items():
            profile = resolve_profile(
                conn, version_id=version_id, name=name, source_roles=[role]
            )
            marks = ",".join("?" for _ in profile["source_ids"])
            rows = conn.execute(
                "SELECT callee_name, count(*) FROM call_instances"
                f" WHERE version_id=? AND source_id IN ({marks})"
                " AND in_target_api=1 AND resolve_kind='unresolved'"
                " GROUP BY callee_name ORDER BY 2 DESC, 1 LIMIT 12",
                (version_id, *profile["source_ids"]),
            ).fetchall()
            unresolved.append(
                {
                    "profile": name,
                    "top_unresolved": [
                        {"api": str(row[0]), "calls": int(row[1])} for row in rows
                    ],
                }
            )
        conn.commit()
        variants[label] = {
            "index": counts,
            "health": health,
            "unresolved": unresolved,
            "macro_calls": _macro_calls(conn, version_id),
            "patterns": {name: sorted(value) for name, value in patterns.items()},
        }
    baseline = variants["baseline"]
    enhanced = variants["enhanced"]
    delta: dict[str, Any] = {}
    per_profile: dict[str, Any] = {}
    for name in PROFILES:
        per_profile[name] = {
            metric: round(
                float(enhanced["health"][name].get(metric, 0.0))
                - float(baseline["health"][name].get(metric, 0.0)),
                4,
            )
            for metric in ("unresolved_rate", "prefix_candidate_rate")
        }
    for metric in ("unresolved_rate", "prefix_candidate_rate"):
        values = [item[metric] for item in per_profile.values()]
        delta[metric] = round(sum(values) / len(values), 4)
    added: dict[str, list[str]] = {}
    target_calls_delta: dict[str, int] = {}
    for name in PROFILES:
        before = set(baseline["patterns"][name])
        after = set(enhanced["patterns"][name])
        added[name] = sorted(after - before)
        before_calls = sum(baseline["health"][name]["target_status"].values())
        after_calls = sum(enhanced["health"][name]["target_status"].values())
        target_calls_delta[name] = after_calls - before_calls
    macro_delta = int(enhanced["macro_calls"]) - int(baseline["macro_calls"])
    return {
        "variants": variants,
        "metric_delta": delta,
        "per_profile_delta": per_profile,
        "target_calls_delta": target_calls_delta,
        "macro_calls_delta": macro_delta,
        "patterns_added": added,
        "improved": any(value > 0 for value in target_calls_delta.values())
        or macro_delta > 0,
        "limitation": (
            "remaining unresolved and prefix calls are listed per profile; "
            "function-like macro bodies are recorded as macro-shape calls "
            "without preprocessor expansion"
        ),
    }


def _gate(name: str, passed: bool, observed: Any, requirement: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "pass" if passed else "fail",
        "observed": observed,
        "requirement": requirement,
    }


def _review(name: str, observed: Any, threshold: Any, status: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "observed": observed,
        "threshold": threshold,
    }


def _quality_gates(
    conn,
    *,
    version: str,
    library: str,
    sources: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
    counts: dict[str, dict[str, Any]],
    facts: list[dict[str, Any]],
    health: dict[str, dict[str, Any]],
    evidence: dict[str, list[dict[str, Any]]],
    patterns: dict[str, set[str]],
    resolution: dict[str, Any],
    version_report: list[dict[str, Any]],
    census: dict[str, Any],
) -> dict[str, Any]:
    from libref.services.code.usage_sources import profile_fingerprint

    gates: list[dict[str, Any]] = []
    roles_by_id = {int(item["source_id"]): item["source_role"] for item in sources}
    source_roles = {item["source_role"] for item in sources}
    gates.append(
        _gate(
            "sources_registered",
            source_roles == {"implementation", "official_example", "reference_project"},
            sorted(source_roles),
            "exactly three declared source roles",
        )
    )
    gates.append(
        _gate(
            "source_commits_and_canonical_urls",
            bool(sources)
            and all(
                item["commit_sha"] and item["source_url"].startswith("https://")
                for item in sources
            ),
            [
                {
                    "source_key": item["source_key"],
                    "commit_sha": item["commit_sha"],
                    "source_url": item["source_url"],
                }
                for item in sources
            ],
            "every source has a pinned commit and an https repository URL",
        )
    )
    missing = {name: item["missing_files"] for name, item in counts.items()}
    gates.append(
        _gate(
            "requested_files_present",
            not any(missing.values()),
            missing,
            "every selected repository-relative file exists at the pinned commit",
        )
    )
    observed_files = {
        name: int(item["counts"].get("files", 0)) for name, item in counts.items()
    }
    dpdk_roles = sum(
        value
        for name, value in observed_files.items()
        if name in ("implementation", "official_example")
    )
    full_corpus = {
        "expected": census["expected"],
        "observed": observed_files,
        "repo_totals": census["repo_totals"],
        "uncovered_dirs": census["uncovered_dirs"],
        "dpdk_partition": {
            "role_files": dpdk_roles,
            "repo_files": census["repo_totals"]["target"],
        },
        "oversized": {
            name: int(item["counts"].get("skipped_oversize", 0))
            for name, item in counts.items()
        },
        "skipped_paths": {
            name: list(item["counts"].get("skipped_paths") or [])
            for name, item in counts.items()
        },
    }
    gates.append(
        _gate(
            "full_corpus_partition",
            observed_files == census["expected"]
            and dpdk_roles == census["repo_totals"]["target"]
            and observed_files["reference_project"]
            == census["repo_totals"]["reference"]
            and not any(full_corpus["oversized"].values()),
            full_corpus,
            "role corpora cover every source file in each pinned repository",
        )
    )
    expected_roles = {
        "implementation": {"implementation"},
        "usage": {"official_example", "reference_project"},
        **{name: {role} for name, role in PROFILES.items()},
    }
    observed_roles = {
        item["name"]: sorted(
            {roles_by_id.get(source_id, "unknown") for source_id in item["source_ids"]}
        )
        for item in profiles
    }
    gates.append(
        _gate(
            "profile_sources_exclusive",
            all(
                set(observed_roles.get(name, [])) == roles
                for name, roles in expected_roles.items()
            ),
            observed_roles,
            "each profile only uses its declared source roles",
        )
    )
    fingerprint_checks = []
    for item in profiles:
        recomputed = profile_fingerprint(
            conn, item["source_ids"], item["support_mode"], item["params"]
        )
        fingerprint_checks.append(
            {
                "profile": item["name"],
                "stored": item["fingerprint"],
                "recomputed": recomputed,
                "matches": recomputed == item["fingerprint"],
            }
        )
    gates.append(
        _gate(
            "profile_fingerprints_match_sources",
            all(item["matches"] for item in fingerprint_checks),
            fingerprint_checks,
            "stored fingerprints match current source identities and rules",
        )
    )
    profile_ids = {item["name"]: set(item["source_ids"]) for item in profiles}
    facts_by_profile = {
        name: _profile_facts(facts, ids) for name, ids in profile_ids.items()
    }
    gates.append(
        _gate(
            "target_calls_present",
            all(item["target_calls"] > 0 for item in facts_by_profile.values()),
            facts_by_profile,
            "every profile has at least one in_target_api call from allowed roles",
        )
    )
    usage_facts = facts_by_profile.get("usage", {})
    gates.append(
        _gate(
            "default_usage_excludes_implementation",
            "implementation" not in usage_facts.get("roles", [])
            and not (
                set(usage_facts.get("mined_status", {}))
                & {TARGET_STATUS_PREFIX, TARGET_STATUS_UNCONFIRMED}
            ),
            {
                **usage_facts,
                "default_usage_patterns": len(patterns["usage"]),
            },
            "default usage has no implementation source and mines no pending-status calls",
        )
    )
    evidence_roles = {
        name: sorted({item["source_role"] for item in groups})
        for name, groups in evidence.items()
    }
    gates.append(
        _gate(
            "named_evidence_roles_exclusive",
            all(
                set(evidence_roles.get(name, [])) <= {roles}
                for name, roles in PROFILES.items()
            ),
            evidence_roles,
            "named profile evidence only contains its declared role",
        )
    )
    gates.append(
        _gate(
            "evidence_identity_and_commit_url",
            bool(evidence)
            and all(
                item.get("source_role")
                and item.get("sample_id")
                and item.get("profile_fingerprint")
                and item.get("canonical_url")
                and "#L" in item.get("canonical_url", "")
                for groups in evidence.values()
                for item in groups
            ),
            {
                name: [
                    {
                        "source_role": item.get("source_role"),
                        "sample_id": item.get("sample_id"),
                        "profile_fingerprint": item.get("profile_fingerprint"),
                        "canonical_url": item.get("canonical_url"),
                    }
                    for item in groups
                ]
                for name, groups in evidence.items()
            },
            "every evidence group carries role, sample, fingerprint and commit URL",
        )
    )
    reference_status = [
        item for item in version_report if item["source_role"] == "reference_project"
    ]
    gates.append(
        _gate(
            "version_compatibility_recorded",
            bool(reference_status)
            and all(
                item["target_status"] == TARGET_STATUS_UNCONFIRMED
                and item["declared_library_version"]
                and item["evidence"]
                for item in reference_status
            ),
            reference_status,
            "reference project declares a target version and mismatch is unconfirmed",
        )
    )
    gates.append(
        _gate(
            "health_is_profile_scoped",
            len({tuple(sorted(item["confidence"].items())) for item in health.values()})
            > 1
            and all("macro_coverage" in item for item in health.values()),
            {
                name: {
                    "confidence": item["confidence"],
                    "unresolved_rate": item["unresolved_rate"],
                    "macro_coverage": item["macro_coverage"],
                }
                for name, item in health.items()
            },
            "profile confidence distributions differ and macro coverage is reported",
        )
    )
    gates.append(
        _gate(
            "scenario_patterns_differ",
            patterns["dpdk-official-examples"] != patterns["dpdk-on-mtl"]
            and not (
                patterns["implementation"]
                & patterns["usage"]
                - (patterns["dpdk-official-examples"] | patterns["dpdk-on-mtl"])
            ),
            {
                "official": len(patterns["dpdk-official-examples"]),
                "mtl": len(patterns["dpdk-on-mtl"]),
                "usage": len(patterns["usage"]),
            },
            "named profiles differ and implementation patterns do not enter usage",
        )
    )
    gates.append(
        _gate(
            "resolution_diff_reported",
            bool(resolution["variants"])
            and set(resolution["metric_delta"])
            == {
                "unresolved_rate",
                "prefix_candidate_rate",
            },
            {
                "metric_delta": resolution["metric_delta"],
                "target_calls_delta": resolution["target_calls_delta"],
                "macro_calls_delta": resolution["macro_calls_delta"],
                "patterns_added": {
                    name: len(value)
                    for name, value in resolution["patterns_added"].items()
                },
                "improved": resolution["improved"],
            },
            "baseline and enhanced header slices report unresolved and prefix deltas",
        )
    )

    reviews = []
    for name, item in health.items():
        for metric in ("unresolved_rate", "prefix_candidate_rate"):
            value = float(item.get(metric, 0.0))
            reviews.append(
                _review(
                    f"{name}.{metric}",
                    value,
                    REVIEW_THRESHOLDS[metric],
                    "pass" if value <= REVIEW_THRESHOLDS[metric] else "needs_review",
                )
            )
    for name, item in health.items():
        ratio = float(item.get("macro_coverage", {}).get("ratio", 0.0))
        reviews.append(
            _review(
                f"{name}.macro_coverage",
                ratio,
                REVIEW_THRESHOLDS["minimum_macro_ratio"],
                "pass"
                if ratio >= REVIEW_THRESHOLDS["minimum_macro_ratio"]
                else "needs_review",
            )
        )
    reviews.append(
        _review(
            "default_usage.prefix_candidate_policy",
            {
                "mined_pending": sum(
                    count
                    for status, count in usage_facts.get("mined_status", {}).items()
                    if status in (TARGET_STATUS_PREFIX, TARGET_STATUS_UNCONFIRMED)
                ),
                "excluded_prefix_calls": usage_facts.get("prefix_calls", 0),
                "excluded_unconfirmed_calls": usage_facts.get("unconfirmed_calls", 0),
            },
            0,
            "pass"
            if not (
                set(usage_facts.get("mined_status", {}))
                & {TARGET_STATUS_PREFIX, TARGET_STATUS_UNCONFIRMED}
            )
            else "needs_review",
        )
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "library": library,
        "version": version,
        "thresholds": REVIEW_THRESHOLDS,
        "gates": gates,
        "reviews": reviews,
        "checks": {item["name"]: item["status"] == "pass" for item in gates},
        "needs_review": [
            item["name"] for item in reviews if item["status"] == "needs_review"
        ],
    }


def render(
    conn, settings: Any, out_dir: Path, *, repos_dir: Path | None = None
) -> dict[str, Any]:
    """Index pinned local repos and render all real-repo review artifacts."""
    manifest, lock = _inputs()
    target = lock["repos"]["target"]
    version = str(target["tag"])
    library = "dpdk-e2e"
    repos_dir = repos_dir or Path(tempfile.mkdtemp(prefix="usage-real-index-"))
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")

    from libref.services.code.mining_read import MiningReadService
    from libref.services.code.mining_refresh import induce_labels, mine_patterns
    from libref.services.code.usage_sources import resolve_profile
    from libref.services.registry.libraries import LibraryService

    counts = _index_version(
        conn,
        settings,
        manifest,
        lock,
        library=library,
        version=version,
        repos_dir=repos_dir,
    )
    _library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version
    )
    patterns: dict[str, set[str]] = {}
    pattern_rows: dict[str, list[dict[str, Any]]] = {}
    for name, role in PROFILES.items():
        resolve_profile(conn, version_id=version_id, name=name, source_roles=[role])
        rows = mine_patterns(
            conn,
            library,
            version,
            **MINE_PARAMS,
            include_ambiguous=True,
            include_unconfirmed=True,
            profile=name,
            source_roles=[role],
        )[:MAX_REPORT_PATTERNS]
        conn.commit()
        asyncio.run(
            induce_labels(
                conn,
                settings,
                library,
                version,
                profile=name,
                include_ambiguous=True,
                include_unconfirmed=True,
                chat=_label_chat,
            )
        )
        patterns[name] = _names(rows)
        pattern_rows[name] = rows
    pattern_rows["implementation"] = mine_patterns(
        conn, library, version, profile="implementation", **MINE_PARAMS
    )[:MAX_REPORT_PATTERNS]
    patterns["implementation"] = _names(pattern_rows["implementation"])
    pattern_rows["usage"] = mine_patterns(
        conn, library, version, profile="usage", **MINE_PARAMS
    )[:MAX_REPORT_PATTERNS]
    patterns["usage"] = _names(pattern_rows["usage"])
    conn.commit()

    resolution = _resolution_diff(
        conn, settings, manifest, lock, repos_dir / "diff", version
    )
    _library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    sources = _source_rows(conn, version_id)
    profiles = _profile_rows(conn, version_id)
    _write(
        out_dir,
        "00-manifest.txt",
        _dump(
            {
                "generated_at": generated_at,
                "library": library,
                "version": version,
                "repos": lock["repos"],
                "input_policy": {
                    "slice": "fixed-commit controlled file slice, not a full-repo survey",
                    "prefix_candidates": "named review profiles only; default usage excludes",
                    "reference_version": "declared dependency must match the target version",
                    "label_text": "deterministic review placeholder, not semantic label quality",
                },
                "index": counts,
            }
        ),
    )
    _write(out_dir, "01-sources.txt", _dump(sources))
    _write(out_dir, "02-profiles.txt", _dump(profiles))
    for idx, name in enumerate(
        ("implementation", "dpdk-official-examples", "dpdk-on-mtl", "usage"), start=3
    ):
        _write(out_dir, f"{idx:02d}-patterns-{name}.txt", _dump(pattern_rows[name]))

    _write(
        out_dir,
        "07-diff-official-vs-mtl.txt",
        _dump(
            {
                "official_only": sorted(
                    patterns["dpdk-official-examples"] - patterns["dpdk-on-mtl"]
                ),
                "mtl_only": sorted(
                    patterns["dpdk-on-mtl"] - patterns["dpdk-official-examples"]
                ),
                "shared": sorted(
                    patterns["dpdk-official-examples"] & patterns["dpdk-on-mtl"]
                ),
                "implementation_pollutes_usage": bool(
                    (patterns["implementation"] & patterns["usage"])
                    - (patterns["dpdk-official-examples"] | patterns["dpdk-on-mtl"])
                ),
            }
        ),
    )
    version_report = _version_report(sources, version)
    _write(out_dir, "08-version-compatibility.txt", _dump(version_report))
    _write(out_dir, "09-resolution-coverage-diff.txt", _dump(resolution))

    reader = MiningReadService(conn, settings)
    evidence: dict[str, list[dict[str, Any]]] = {}
    health: dict[str, dict[str, Any]] = {}
    labels_report: dict[str, Any] = {}
    for idx, name in enumerate(PROFILES, start=10):
        role = PROFILES[name]
        evidence_pattern = next(
            (
                item["pattern"]
                for item in pattern_rows[name]
                if any(part.startswith("rte_") for part in item.get("pattern", []))
            ),
            pattern_rows[name][0]["pattern"] if pattern_rows[name] else [],
        )
        evidence[name] = reader.pattern_evidence(
            evidence_pattern,
            library=library,
            version=version,
            limit=5,
            with_snippet=False,
            include_ambiguous=True,
            include_unconfirmed=True,
            profile=name,
        )
        _write(out_dir, f"{idx:02d}-evidence-{name}.txt", _dump(evidence[name]))
        health[name] = reader.mining_health(library, version, profile=name)
        _write(out_dir, f"{idx + 10:02d}-health-{name}.txt", _dump(health[name]))
        labels = reader.list_labels(library, version, limit=5, profile=name)
        from libref.services.docs.answer_materials import build_pattern_block

        labels_report[name] = {
            "source_hint": next(
                (item["source_hint"] for item in labels if item.get("source_hint")),
                "",
            ),
            "review_note": (
                "deterministic review placeholder; not semantic label quality "
                "or an official recommendation; prefix candidates and "
                "version-unconfirmed calls are included only in named review profiles"
            ),
            "labels": labels,
            "answer_block": build_pattern_block(labels, 5),
        }
        _write(out_dir, f"{idx + 20:02d}-labels-{name}.txt", _dump(labels_report[name]))

    comparison = {
        name: {
            "unresolved_rate": item["unresolved_rate"],
            "prefix_candidate_rate": item["prefix_candidate_rate"],
            "macro_coverage": item["macro_coverage"],
            "target_status": item["target_status"],
            "confidence": item["confidence"],
            "resolve_kinds": item["resolve_kinds"],
        }
        for name, item in health.items()
    }
    _write(out_dir, "40-health-comparison.txt", _dump(comparison))

    corpus_census = _corpus_census(manifest, lock)
    gates = _quality_gates(
        conn,
        version=version,
        library=library,
        sources=sources,
        profiles=profiles,
        counts=counts,
        facts=_call_facts(conn, version_id),
        health=health,
        evidence=evidence,
        patterns=patterns,
        resolution=resolution,
        version_report=version_report,
        census=corpus_census,
    )
    _write(out_dir, "50-quality-gates.txt", _dump(gates))

    slice_summary = {}
    for name, item in counts.items():
        repo = manifest["corpora"][name]["repo"]
        slice_summary[name] = {
            "repository": manifest[repo]["name"],
            "tag": manifest[repo]["tag"],
            "commit": item["commit"],
            "files": int(item["counts"].get("files", 0)),
            "selection": item["selection"],
            "missing_files": item["missing_files"],
        }
    summary = {
        "generated_at": generated_at,
        "library": library,
        "version": version,
        "sources": len(sources),
        "profiles": len(profiles),
        "patterns": {key: len(value) for key, value in patterns.items()},
        "checks": gates["checks"],
        "needs_review": gates["needs_review"],
        "corpus": {
            "scope": "complete source-file partition of pinned repositories by role",
            "files": slice_summary,
            "repo_totals": corpus_census["repo_totals"],
            "not_covered": (
                "non-source assets, driver/application directories outside the "
                "role corpora, and full macro expansion; uncovered top-level "
                "directories are listed by each gate"
            ),
            "uncovered_dirs": corpus_census["uncovered_dirs"],
        },
        "health_comparison": comparison,
        "version_compatibility": version_report,
        "resolution_coverage": {
            "metric_delta": resolution["metric_delta"],
            "target_calls_delta": resolution["target_calls_delta"],
            "macro_calls_delta": resolution["macro_calls_delta"],
            "improved": resolution["improved"],
            "note": (
                "baseline is the old controlled slice; full-corpus resolution "
                "deltas, remaining unresolved APIs and macro bodies are in "
                "09-resolution-coverage-diff.txt"
            ),
        },
    }
    _write(out_dir, "SUMMARY.txt", _dump(summary))
    return summary


def main() -> int:
    from libref.config import LibrefSettings
    from libref.db import apply_migrations, open_db

    args = _parse_args()
    _inputs()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "usage-real.db"
        conn = open_db(db_path)
        apply_migrations(conn)
        settings = LibrefSettings(
            db_path=str(db_path),
            discovery_offline=True,
            registry_offline=True,
            induction_enabled=True,
            simvec_dir=str(Path(tmp) / "simvec"),
        )
        try:
            summary = render(conn, settings, Path(args.out_dir))
        finally:
            conn.close()
    print(_dump(summary))
    return 0 if all(summary["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
