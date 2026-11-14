"""Source-code commands: index, mine, and usage insights."""

from __future__ import annotations

import argparse
import sys

from libref.cli.common import (
    _build_vector,
    _connect,
    _load_settings,
    _print_job,
    _registry_overrides,
)
from libref.services.code.code_ingest import (
    INDEX_REPO_JOB_KIND,
    build_repo_runner,
    index_repo,
)
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_refresh import (
    MINE_PATTERNS_JOB_KIND,
    build_mine_runner,
    mine_job_params,
    mine_patterns,
)
from libref.services.infra.jobs import JobManager


def cmd_index_repo(args: argparse.Namespace) -> int:
    import asyncio

    from libref.crawler.registry import RegistryError

    settings = _load_settings(args, _registry_overrides(args))
    conn = _connect(settings)
    repos_dir = settings.repos_dir
    vec_store, embed = _build_vector(settings)
    try:
        if args.source is not None:
            if args.version is None:
                print("explicit mode requires library version source", file=sys.stderr)
                return 2
            if args.ecosystem is not None:
                print("--ecosystem and source are mutually exclusive", file=sys.stderr)
                return 2
            library, ecosystem = args.library.strip(), "custom"
            version, source = args.version, args.source
            resolved_from, registry_confirmed = "explicit", True
        else:
            try:

                async def _resolve():
                    from libref.crawler.http import build_async_client
                    from libref.services.registry.resolver import resolve

                    async with build_async_client(settings) as client:
                        return await resolve(
                            conn,
                            client,
                            settings,
                            args.ecosystem,
                            args.library,
                            args.version,
                            "repo",
                            bool(getattr(args, "include_prerelease", False)),
                        )

                resolution = asyncio.run(_resolve())
            except ValueError as exc:
                print(f"invalid: {exc}", file=sys.stderr)
                return 2
            except RegistryError as exc:
                from libref.cli.common import format_registry_error

                print(
                    format_registry_error(exc, target="repo"),
                    file=sys.stderr,
                )
                return 1
            assert resolution.repo_url is not None
            library, version, source = (
                resolution.name,
                resolution.version,
                resolution.repo_url,
            )
            ecosystem = resolution.ecosystem
            resolved_from = resolution.source
            registry_confirmed = resolution.registry_confirmed
            print(
                f"resolved {ecosystem}:{library} {version} -> {source} [{resolved_from}]"
            )
        if args.run_async:
            mgr = JobManager(conn, stale_after=float(settings.heartbeat_timeout))
            mgr.recover()
            job_id = mgr.submit(
                INDEX_REPO_JOB_KIND,
                {
                    "library": library,
                    "version": version,
                    "source": source,
                    "update": bool(args.update),
                    "ecosystem": ecosystem,
                    "resolved_from": resolved_from,
                    "registry_confirmed": registry_confirmed,
                    "fresh": bool(getattr(args, "fresh", False)),
                    "subdir": getattr(args, "subdir", None),
                    "only_files": list(getattr(args, "only_file", None) or []) or None,
                    "max_files": getattr(args, "max_files", None),
                },
            )

            async def _drive():
                mgr.start_worker(
                    build_repo_runner(settings, conn, repos_dir, vec_store, embed, mgr)
                )
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            _print_job(job)
            return 0 if job.status == "done" else 1
        try:
            counts = index_repo(
                conn,
                library,
                version,
                source,
                update=bool(args.update),
                repos_dir=repos_dir,
                ecosystem=ecosystem,
                resolved_from=resolved_from,
                registry_confirmed=registry_confirmed,
                repos_quota_mb=int(settings.repos_quota_mb),
                settings=settings,
                vec_store=vec_store,
                embed=embed,
                fresh=bool(getattr(args, "fresh", False)),
                subdir=getattr(args, "subdir", None),
                only_files=list(getattr(args, "only_file", None) or []) or None,
                max_files=getattr(args, "max_files", None),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 — any fetch failure exits 1 without a traceback
            print(f"failed: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    import asyncio
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    cluster_threshold = (
        args.cluster_threshold
        if args.cluster_threshold is not None
        else settings.cluster_threshold
    )
    cluster_min_size = (
        args.cluster_min_size
        if args.cluster_min_size is not None
        else settings.cluster_min_size
    )
    try:
        kinds = (
            args.resolve_kinds.split(",")
            if getattr(args, "resolve_kinds", None)
            else None
        )
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                MINE_PATTERNS_JOB_KIND,
                mine_job_params(
                    args.library,
                    args.version,
                    ecosystem=getattr(args, "ecosystem", None),
                    minsup=args.minsup,
                    topn=args.topn,
                    maxlen=args.maxlen,
                    minlen=args.minlen,
                    include_ambiguous=bool(args.include_ambiguous),
                    cluster_threshold=cluster_threshold,
                    cluster_min_size=cluster_min_size,
                    resolve_kinds=kinds,
                    min_args=getattr(args, "min_args", None),
                    max_args=getattr(args, "max_args", None),
                ),
            )

            async def _drive():
                mgr.start_worker(build_mine_runner(settings, conn))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            _print_job(job)
            return 0 if job.status == "done" else 1
        results = mine_patterns(
            conn,
            args.library,
            args.version,
            ecosystem=getattr(args, "ecosystem", None),
            minsup=args.minsup,
            topn=args.topn,
            maxlen=args.maxlen,
            minlen=args.minlen,
            include_ambiguous=bool(args.include_ambiguous),
            cluster_threshold=cluster_threshold,
            cluster_min_size=cluster_min_size,
            resolve_kinds=kinds,
            min_args=getattr(args, "min_args", None),
            max_args=getattr(args, "max_args", None),
        )
    finally:
        conn.close()
    for item in results:
        print(f"{item['support']} {json.dumps(item['pattern'], ensure_ascii=False)}")
    return 0


def cmd_repo_deps(args: argparse.Namespace) -> int:
    import json

    from libref.services.code.code_ingest import list_repo_dependencies

    conn = _connect(_load_settings(args))
    try:
        try:
            rows = list_repo_dependencies(
                conn,
                args.library,
                args.version,
                getattr(args, "dep_ecosystem", None),
                ecosystem=getattr(args, "ecosystem", None),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_induce_labels(args: argparse.Namespace) -> int:
    import asyncio
    import json as _json

    from libref.services.code.mining_refresh import (
        INDUCE_LABELS_JOB_KIND,
        build_induce_runner,
        induce_job_params,
        induce_labels,
    )

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                INDUCE_LABELS_JOB_KIND,
                induce_job_params(
                    args.library,
                    args.version,
                    ecosystem=getattr(args, "ecosystem", None),
                    topn=args.topn,
                ),
            )

            async def _drive():
                mgr.start_worker(build_induce_runner(settings, conn))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            _print_job(job)
            return 0 if job.status == "done" else 1
        out = asyncio.run(
            induce_labels(
                conn,
                settings,
                args.library,
                args.version,
                ecosystem=getattr(args, "ecosystem", None),
                topn=args.topn,
            )
        )
    finally:
        conn.close()
    print(_json.dumps(out, ensure_ascii=False))
    return 0


def cmd_api_insight(args: argparse.Namespace) -> int:
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            apis = args.apis.split(",") if getattr(args, "apis", None) else None
            rows = MiningReadService(conn, settings).api_insight(
                args.view,
                getattr(args, "api", None),
                apis=apis,
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_pattern_insight(args: argparse.Namespace) -> int:
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        containing = (
            args.containing.split(",") if getattr(args, "containing", None) else None
        )
        pattern = args.pattern.split(",") if getattr(args, "pattern", None) else None
        if getattr(args, "dedup", False) and getattr(args, "no_dedup", False):
            print(
                "invalid: --dedup and --no-dedup are mutually exclusive",
                file=sys.stderr,
            )
            return 2
        if getattr(args, "dedup", False):
            dedup_value = True
        elif getattr(args, "no_dedup", False):
            dedup_value = False
        else:
            dedup_value = None
        try:
            rows = MiningReadService(conn, settings).pattern_insight(
                args.view,
                pattern=pattern,
                containing=containing,
                prefix=getattr(args, "prefix", None),
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
                per_file_limit=getattr(args, "per_file_limit", None),
                dedup=dedup_value,
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_symbol_insight(args: argparse.Namespace) -> int:
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            apis = args.apis.split(",") if getattr(args, "apis", None) else None
            rows = MiningReadService(conn, settings).symbol_insight(
                args.view,
                getattr(args, "api", None),
                apis=apis,
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
                offset=int(getattr(args, "offset", 0) or 0),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_call_insight(args: argparse.Namespace) -> int:
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            kinds = (
                args.resolve_kinds.split(",")
                if getattr(args, "resolve_kinds", None)
                else None
            )
            rows = MiningReadService(conn, settings).call_insight(
                args.view,
                getattr(args, "api", None),
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
                offset=int(getattr(args, "offset", 0) or 0),
                include_ambiguous=bool(getattr(args, "include_ambiguous", False)),
                resolve_kinds=kinds,
                min_args=getattr(args, "min_args", None),
                max_args=getattr(args, "max_args", None),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_import_insight(args: argparse.Namespace) -> int:
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            rows = MiningReadService(conn, settings).import_insight(
                args.view,
                getattr(args, "api", None),
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
                offset=int(getattr(args, "offset", 0) or 0),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_mining_health(args: argparse.Namespace) -> int:
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            health = MiningReadService(conn, settings).mining_health(
                library=getattr(args, "library", None),
                version=getattr(args, "version", None),
                ecosystem=getattr(args, "ecosystem", None),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    print(json.dumps(health, ensure_ascii=False))
    return 0


def cmd_probe_repo(args: argparse.Namespace) -> int:
    """Read-only repository probe: directory shape without parsing."""
    import json as _json

    from libref.services.code.probe import probe_repo

    settings = _load_settings(args)
    try:
        probe = probe_repo(
            args.source,
            settings=settings,
            subdir=getattr(args, "subdir", None),
            max_files=getattr(args, "max_files", None),
            sample_limit=getattr(args, "sample_limit", None),
            only_files=list(getattr(args, "only_file", None) or []) or None,
        )
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — probe failures stay readable without a traceback
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    print(_json.dumps(probe.model_dump(), ensure_ascii=False))
    return 0
