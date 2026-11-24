"""Report rendering for real usage-mining runs."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tests.e2e.usage_mining.config import (
    MAX_REPORT_PATTERNS,
    MINE_PARAMS,
    PROFILES,
)
from tests.e2e.usage_mining.facts import (
    _call_facts,
    _corpus_census,
    _dump,
    _index_version,
    _inputs,
    _label_chat,
    _macro_body_call_names,
    _macro_evidence,
    _macro_pattern_names,
    _names,
    _profile_rows,
    _resolution_diff,
    _source_rows,
    _version_report,
    _write,
)
from tests.e2e.usage_mining.quality_gates import _quality_gates


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
    macro_names = _macro_pattern_names(conn, version_id)
    sources = _source_rows(conn, version_id)
    macro_body_names = _macro_body_call_names(
        conn,
        version_id,
        [
            source["source_id"]
            for source in sources
            if source["source_role"] == "reference_project"
        ],
    )
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
    version_report = _version_report(conn, sources, version, version_id)
    _write(out_dir, "08-version-compatibility.txt", _dump(version_report))
    _write(out_dir, "09-resolution-coverage-diff.txt", _dump(resolution))

    reader = MiningReadService(conn, settings)
    evidence: dict[str, list[dict[str, Any]]] = {}
    macro_evidence_by_profile: dict[str, list[dict[str, Any]]] = {}
    health: dict[str, dict[str, Any]] = {}
    labels_report: dict[str, Any] = {}
    for idx, name in enumerate(PROFILES, start=10):
        role = PROFILES[name]
        prefer_macro = name == "dpdk-on-mtl" and bool(macro_body_names)
        evidence_pattern = next(
            (
                item["pattern"]
                for item in pattern_rows[name]
                if any(part.startswith("rte_") for part in item.get("pattern", []))
                and (
                    not prefer_macro
                    or any(part in macro_body_names for part in item.get("pattern", []))
                )
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
        macro_evidence_by_profile[name] = _macro_evidence(evidence[name])
        if name == "dpdk-on-mtl":
            _write(
                out_dir,
                "12-macro-evidence-dpdk-on-mtl.txt",
                _dump(macro_evidence_by_profile[name]),
            )
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
    _write(
        out_dir,
        "41-macro-metrics.txt",
        _dump({name: item["macro_coverage"] for name, item in health.items()}),
    )
    _write(
        out_dir,
        "60-macro-fingerprint.txt",
        _dump(
            {
                "target_macro_definitions": len(macro_names),
                "macro_names": sorted(macro_names),
                "profiles": [
                    {
                        "name": item["name"],
                        "macro_policy": (item.get("params") or {}).get("macro_policy"),
                        "fingerprint": item["fingerprint"],
                    }
                    for item in profiles
                ],
                "version_report": version_report,
            }
        ),
    )

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
        macro_pattern_names=macro_names,
        macro_evidence_by_profile=macro_evidence_by_profile,
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
        "macro_metrics": {
            name: item["macro_coverage"] for name, item in health.items()
        },
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
