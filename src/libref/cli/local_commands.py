"""Server-local commands for direct indexing, probing and mining."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from libref.cli.common import _build_vector, _connect, _load_settings
from libref.errors import invalid_argument


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _repeat(values: list[str] | None) -> list[str] | None:
    return list(values) if values else None


def cmd_index_repo(args: argparse.Namespace) -> int:
    """Index a repository from a host path; remote sources use index requests."""
    from libref.services.code.code_ingest import index_repo

    source = str(args.source)
    if source.startswith(("http://", "https://", "git+")):
        raise invalid_argument(
            "remote repositories must be submitted with librefctl request submit",
            field="source",
        )
    settings = _load_settings(args)
    vec_store, embed = _build_vector(settings)
    conn = _connect(settings)
    try:
        counts = index_repo(
            conn,
            args.library,
            args.version,
            source,
            update=bool(args.update),
            repos_dir=Path(settings.repos_dir),
            ecosystem=args.ecosystem or "custom",
            resolved_from="explicit",
            registry_confirmed=False,
            repos_quota_mb=settings.repos_quota_mb,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
            fresh=bool(args.fresh),
            subdir=args.subdir,
            only_files=_repeat(args.only_file),
            exclude_patterns=_repeat(args.exclude_pattern),
            max_files=args.max_files,
            source_role=args.source_role,
            target_filter={
                "prefixes": _repeat(args.target_prefix) or [],
                "names": _repeat(args.target_name) or [],
            },
        )
    finally:
        conn.close()
    for outcome, count in counts.items():
        print(f"{outcome}: {count}")
    return 0


def cmd_probe_repo(args: argparse.Namespace) -> int:
    """Inspect a repository layout without parsing or storing it."""
    from libref.services.code.probe import probe_repo

    settings = _load_settings(args)
    probe = probe_repo(
        args.source,
        settings=settings,
        subdir=args.subdir,
        max_files=args.max_files,
        sample_limit=args.sample_limit,
        only_files=_repeat(args.only_file),
        exclude_patterns=_repeat(args.exclude_pattern),
        tree_depth=args.tree_depth,
        samples_per_dir=args.samples_per_dir,
    )
    _print_json(probe.model_dump())
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    """Run sequence mining directly against local source evidence."""
    from libref.services.code.mining_refresh import (
        DEFAULT_MAXLEN,
        DEFAULT_MINLEN,
        DEFAULT_MINSUP,
        mine_patterns,
    )

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        results = mine_patterns(
            conn,
            args.library,
            args.version,
            ecosystem=args.ecosystem,
            minsup=DEFAULT_MINSUP if args.minsup is None else args.minsup,
            topn=args.topn,
            maxlen=DEFAULT_MAXLEN if args.maxlen is None else args.maxlen,
            minlen=DEFAULT_MINLEN if args.minlen is None else args.minlen,
            include_ambiguous=bool(args.include_ambiguous),
            cluster_threshold=(
                args.cluster_threshold
                if args.cluster_threshold is not None
                else settings.cluster_threshold
            ),
            cluster_min_size=(
                args.cluster_min_size
                if args.cluster_min_size is not None
                else settings.cluster_min_size
            ),
            resolve_kinds=args.resolve_kind,
            min_args=args.min_args,
            max_args=args.max_args,
            profile=args.profile,
            source_roles=args.source_role,
            macro_policy=args.macro_policy,
        )
    finally:
        conn.close()
    for item in results:
        print(f"{item['support']} {json.dumps(item['pattern'], ensure_ascii=False)}")
    return 0


def cmd_induce_labels(args: argparse.Namespace) -> int:
    """Induce labels directly with the locally configured chat endpoint."""
    from libref.services.code.mining_refresh import induce_labels

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        out = asyncio.run(
            induce_labels(
                conn,
                settings,
                args.library,
                args.version,
                ecosystem=args.ecosystem,
                topn=args.topn,
                include_ambiguous=bool(args.include_ambiguous),
                profile=args.profile,
                macro_policy=args.macro_policy,
            )
        )
    finally:
        conn.close()
    _print_json(out)
    return 0


def cmd_doc_features(args: argparse.Namespace) -> int:
    """Rebuild document-derived features for one stored version."""
    from libref.services.docs.feature_refresh import refresh_doc_features

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        counts = refresh_doc_features(
            conn,
            args.library,
            args.version,
            ecosystem=args.ecosystem,
            profile=args.profile,
            scope=args.scope,
            settings=settings,
        )
    finally:
        conn.close()
    _print_json(counts)
    return 0
