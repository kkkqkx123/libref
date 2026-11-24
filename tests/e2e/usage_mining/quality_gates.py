"""Quality gates for real usage-mining runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from libref.astminer.models import (
    TARGET_STATUS_PREFIX,
    TARGET_STATUS_UNCONFIRMED,
)
from tests.e2e.usage_mining.config import (
    PROFILES,
    REVIEW_THRESHOLDS,
)
from tests.e2e.usage_mining.facts import _gate, _profile_facts, _review


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
    macro_pattern_names: set[str],
    macro_evidence_by_profile: dict[str, list[dict[str, Any]]],
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
    policy_by_profile = {
        str(item["name"]): str((item.get("params") or {}).get("macro_policy") or "")
        for item in profiles
    }
    gates.append(
        _gate(
            "macro_policy_scoped",
            all(policy_by_profile.get(name) == "review_only" for name in PROFILES)
            and policy_by_profile.get("usage") == "exclude"
            and policy_by_profile.get("implementation") == "exclude",
            policy_by_profile,
            "named review profiles allow macro evidence; default profiles exclude it",
        )
    )
    gates.append(
        _gate(
            "named_profiles_mine_macro_calls",
            all(facts_by_profile[name]["macro_calls"] > 0 for name in PROFILES)
            and all(patterns[name] & macro_pattern_names for name in PROFILES),
            {
                name: {
                    "macro_calls": facts_by_profile[name]["macro_calls"],
                    "macro_patterns": sorted(patterns[name] & macro_pattern_names),
                }
                for name in PROFILES
            },
            "each named profile carries macro calls into mined patterns",
        )
    )
    gates.append(
        _gate(
            "default_usage_excludes_macro_calls",
            not (patterns["usage"] & macro_pattern_names),
            {
                "macro_calls": usage_facts["macro_calls"],
                "macro_patterns": sorted(patterns["usage"] & macro_pattern_names),
            },
            "default usage mines no macro patterns from unapproved macro calls",
        )
    )
    gates.append(
        _gate(
            "macro_calls_stay_inferred",
            all(
                "EXTRACTED" not in facts_by_profile[name]["macro_confidence"]
                for name in PROFILES
            ),
            {name: facts_by_profile[name]["macro_confidence"] for name in PROFILES},
            "macro-body calls never carry EXTRACTED confidence",
        )
    )
    required_metrics = {
        "macro_calls",
        "target_calls",
        "target_macro_ratio",
        "target_macro_definitions",
        "covered_macro_definitions",
        "definition_coverage",
        "resolved_macro_calls",
        "macro_resolution_ratio",
        "confidence",
        "unresolved",
    }
    gates.append(
        _gate(
            "macro_metrics_reviewable",
            all(
                required_metrics <= set(item["macro_coverage"])
                and item["macro_coverage"]["target_macro_definitions"] > 0
                for item in health.values()
            ),
            {
                name: {
                    key: item["macro_coverage"].get(key)
                    for key in sorted(required_metrics)
                }
                for name, item in health.items()
            },
            "health reports target-relative ratios, definition coverage and unresolved names",
        )
    )
    gates.append(
        _gate(
            "macro_evidence_located",
            bool(macro_evidence_by_profile.get("dpdk-on-mtl"))
            and all(
                call.get("macro_body") is True
                and call.get("macro_host")
                and call.get("definition_url")
                and "macro_body" in group.get("rendered", "")
                for group in macro_evidence_by_profile["dpdk-on-mtl"]
                for call in group.get("calls", [])
                if call.get("relation_kind") == "macro"
            ),
            {
                name: [
                    {
                        "macro_host": call.get("macro_host"),
                        "definition_url": call.get("definition_url"),
                    }
                    for group in groups
                    for call in group.get("calls", [])
                    if call.get("relation_kind") == "macro"
                ]
                for name, groups in macro_evidence_by_profile.items()
            },
            "macro evidence names the host macro and links to its definition",
        )
    )
    gates.append(
        _gate(
            "macro_body_fingerprints_recorded",
            bool(macro_pattern_names)
            and all(
                item["params"].get("macro_policy") and item["fingerprint"]
                for item in profiles
            ),
            {
                "macro_definitions": len(macro_pattern_names),
                "policies": policy_by_profile,
            },
            "macro bodies and policies participate in stored profile identities",
        )
    )
    gates.append(
        _gate(
            "version_macro_body_report",
            all("macro_body_diff" in item for item in version_report)
            and any(
                item["target_status"] != "version_unconfirmed"
                or item["macro_body_status"] == "version_unconfirmed"
                for item in version_report
            ),
            [
                {
                    "source_key": item["source_key"],
                    "target_status": item["target_status"],
                    "macro_body_status": item["macro_body_status"],
                    "changed_macros": len(item["macro_body_diff"]["changed"]),
                }
                for item in version_report
            ],
            "version report lists macro body differences and keeps version uncertainty",
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
        coverage = item.get("macro_coverage", {})
        target_ratio = float(coverage.get("target_macro_ratio", 0.0))
        definition_coverage = float(coverage.get("definition_coverage", 0.0))
        reviews.append(
            _review(
                f"{name}.macro_target_ratio",
                {
                    "ratio": target_ratio,
                    "missing": [item["api"] for item in coverage.get("unresolved", [])][
                        :12
                    ],
                },
                REVIEW_THRESHOLDS["macro_target_call_ratio"],
                "pass"
                if target_ratio >= REVIEW_THRESHOLDS["macro_target_call_ratio"]
                else "needs_review",
            )
        )
        reviews.append(
            _review(
                f"{name}.macro_definition_coverage",
                {
                    "coverage": definition_coverage,
                    "missing": [item["api"] for item in coverage.get("unresolved", [])][
                        :12
                    ],
                },
                REVIEW_THRESHOLDS["macro_definition_coverage"],
                "pass"
                if definition_coverage >= REVIEW_THRESHOLDS["macro_definition_coverage"]
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
