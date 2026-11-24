"""Fact extraction helpers for real usage-mining runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from libref.astminer.models import (
    TARGET_STATUS_PREFIX,
    TARGET_STATUS_RESOLVED,
    TARGET_STATUS_UNCONFIRMED,
)
from tests.e2e.usage_mining.config import (
    LOCK,
    MANIFEST,
    MAX_REPORT_PATTERNS,
    MINE_PARAMS,
    PROFILES,
    ROOT,
)


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
        "SELECT id, version_id, source_key, source_role, origin_kind, source_url,"
        " commit_sha, selection, target_filter FROM code_sources"
        " WHERE version_id=? ORDER BY id",
        (version_id,),
    ).fetchall()
    return [
        {
            "source_id": int(row[0]),
            "version_id": int(row[1]),
            "source_key": str(row[2]),
            "source_role": str(row[3]),
            "origin_kind": str(row[4]),
            "source_url": str(row[5] or ""),
            "commit_sha": str(row[6] or ""),
            "selection": json.loads(str(row[7])) if row[7] else {},
            "target_filter": json.loads(str(row[8])) if row[8] else {},
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
        " confidence, COALESCE(relation_kind,'direct'), count(*) FROM call_instances"
        " WHERE version_id=? GROUP BY source_id, source_role, in_target_api,"
        " target_status, resolve_kind, confidence, 7 ORDER BY source_id, target_status",
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
            "relation_kind": str(row[6]),
            "calls": int(row[7]),
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
        "macro_calls": sum(
            int(row["calls"]) for row in allowed if row["relation_kind"] == "macro"
        ),
        "macro_confidence": sorted(
            {row["confidence"] for row in allowed if row["relation_kind"] == "macro"}
        ),
    }


def _version_report(
    conn,
    source_rows: list[dict[str, Any]],
    version: str,
    target_version_id: int,
) -> list[dict[str, Any]]:
    from libref.services.code.usage_sources import (
        macro_body_diff,
        source_target_status,
    )

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
        diff = macro_body_diff(conn, source["version_id"], target_version_id)
        changed = bool(diff["changed"] or diff["added"] or diff["removed"])
        if status:
            macro_status = "version_unconfirmed"
        elif changed:
            macro_status = "macro_body_changed"
        else:
            macro_status = "macro_body_confirmed"
        out.append(
            {
                "source_key": source["source_key"],
                "source_role": source["source_role"],
                "target_version": version,
                "declared_library_version": declared,
                "target_status": status or "version_confirmed",
                "evidence": str(filt.get("version_evidence") or ""),
                "macro_body_status": macro_status,
                "macro_body_diff": diff,
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


def _macro_pattern_names(conn, version_id: int) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT qualified_name FROM api_entities WHERE version_id=? AND kind='macro'",
            (version_id,),
        ).fetchall()
    }


def _macro_body_call_names(conn, version_id: int, source_ids: list[int]) -> set[str]:
    if not source_ids:
        return set()
    marks = ",".join("?" for _ in source_ids)
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT callee_name FROM call_instances"
            f" WHERE version_id=? AND source_id IN ({marks})"
            " AND relation_kind='macro'",
            (version_id, *source_ids),
        ).fetchall()
    }


def _macro_evidence(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        group
        for group in groups
        if any(call.get("relation_kind") == "macro" for call in group.get("calls", []))
    ]


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
