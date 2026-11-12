"""Operations commands: database, vectors, jobs, retention, serving."""

from __future__ import annotations

import argparse
import sys

from libref.cli.common import (
    _build_vector,
    _connect,
    _load_settings,
    _mcp_tools_overrides,
    applied_count,
)
from libref.config import redacted_dict, resolve_config_file, settings_fingerprint
from libref.services.factory import build_maintenance_service
from libref.services.infra.jobs import JobManager
from libref.services.ops.review import ReviewService


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    conn = _connect(settings)
    applied = applied_count(conn)
    conn.close()
    print(f"db ready at {settings.db_path} ({applied} migrations applied)")
    return 0


def cmd_reembed(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        vec_store, embed = _build_vector(settings)
        if vec_store is None or embed is None:
            print("embedding is not configured (missing key)", file=sys.stderr)
            return 1
        svc, _gw = build_maintenance_service(
            conn, settings, vec_store=vec_store, embed=embed
        )
        out = svc.reembed_library(
            args.library,
            args.version,
            ecosystem=args.ecosystem,
        )
    finally:
        conn.close()
    print(f"docs: {out['docs']} upserted: {out['upserted']}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    import asyncio as _asyncio

    from libref.services.ops.maintenance import reconcile_version
    from libref.services.registry.libraries import mining_version_ids

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        vec_store, embed = _build_vector(settings)
        if vec_store is None or embed is None:
            print("embedding is not configured (missing key)", file=sys.stderr)
            return 1
        vids = mining_version_ids(conn, args.library, args.version, args.ecosystem)
        if len(vids) != 1:
            print(
                "invalid: library version must resolve to a single version",
                file=sys.stderr,
            )
            return 1
        out = _asyncio.run(reconcile_version(conn, settings, vec_store, embed, vids[0]))
    finally:
        conn.close()
    print(
        f"sqlite: {out['sqlite_embeddable']} vector_before: {out['vector_before']} "
        f"missing: {out['missing']} orphans: {out['orphans']} "
        f"upserted: {out['upserted']} rebuilt: {out['rebuilt']}"
    )
    return 0


def cmd_fts_rebuild(args: argparse.Namespace) -> int:
    from libref.services.ops.maintenance import rebuild_fts

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        out = rebuild_fts(conn)
    finally:
        conn.close()
    print(
        f"documents: {out['documents']} sampled: {out['sampled']} "
        f"missing: {out['missing']} in_sync: {out['in_sync']}"
    )
    return 0 if out["in_sync"] else 1


def cmd_fts_check(args: argparse.Namespace) -> int:
    from libref.services.ops.maintenance import check_fts

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        out = check_fts(conn)
    finally:
        conn.close()
    print(
        f"documents: {out['documents']} triggers: {out['triggers']} "
        f"sampled: {out['sampled']} missing: {out['missing']} "
        f"in_sync: {out['in_sync']}"
    )
    return 0 if out["in_sync"] else 1


def cmd_review(args: argparse.Namespace) -> int:
    conn = _connect(_load_settings(args))
    try:
        svc = ReviewService(conn)
        action = args.review_action
        try:
            if action == "list":
                result = svc.apply(action, limit=args.limit)
            else:
                result = svc.apply(action, args.entity_id)
        except ValueError as exc:
            text = str(exc)
            if "cannot transition" in text or "cannot reopen" in text:
                print(f"conflict: {text}", file=sys.stderr)
            else:
                print(f"invalid: {text}", file=sys.stderr)
            return 1
        if action == "list":
            for r in result:
                print(
                    f"{r['entity_id']} {r['confidence']} {r['callee']} "
                    f"{r['caller_file']}:{r['line_start']}-{r['line_end']}"
                )
            return 0
    finally:
        conn.close()
    print("ok")
    return 0


def cmd_job_show(args: argparse.Namespace) -> int:
    import json as _json

    conn = _connect(_load_settings(args))
    try:
        mgr = JobManager(conn)
        if getattr(args, "list", False) or getattr(args, "job_id", None) is None:
            out = mgr.show(None, limit=args.limit)
            for job in out["jobs"]:
                print(
                    f"{job['id']} {job['kind']} {job['status']} {job['finished_at'] or ''}"
                )
            return 0
        out = mgr.show(args.job_id)
        if "error" in out and "id" not in out:
            print(f"(unknown job: {args.job_id})", file=sys.stderr)
            return 1
        print(
            f"job {out['id']} {out['status']}"
            + (f" error={out['error']}" if out.get("error") else "")
        )
        if out.get("result"):
            print(f"result: {_json.dumps(out['result'], ensure_ascii=False)[:2000]}")
            if args.full:
                print(_json.dumps(out["result"], ensure_ascii=False))
        if out.get("finished_at"):
            print(f"finished_at: {out['finished_at']}")
        return 0
    finally:
        conn.close()


def cmd_purge(args: argparse.Namespace) -> int:
    from libref.services.ops.retention import purge_version

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        vec_store, _embed = _build_vector(settings)
        out = purge_version(
            conn,
            args.library,
            args.version,
            ecosystem=getattr(args, "ecosystem", None),
            repos_dir=settings.repos_dir,
            snapshot_root=settings.snapshot_dir,
            vec_store=vec_store,
        )
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for key, value in out.items():
        print(f"{key}: {value}")
    return 0


def cmd_deletions(args: argparse.Namespace) -> int:
    import json as _json

    from libref.services.ops.retention import list_deletions, prune_expired_logs

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        if getattr(args, "prune", False):
            ttl = getattr(args, "ttl_days", None)
            if ttl is None:
                ttl = float(settings.deletion_log_ttl_days)
            removed = prune_expired_logs(conn, float(ttl))
            print(f"pruned: {removed}")
            return 0
        rows = list_deletions(conn, limit=int(getattr(args, "limit", 50)))
        if not rows:
            print("(no deletions)")
            return 0
        for row in rows:
            print(
                f"{row['id']} {row['ecosystem']}:{row['library']} {row['version']} {row['reason']} {row['created_at']}"
            )
            if getattr(args, "full", False):
                print(f"    {_json.dumps(row['detail'], ensure_ascii=False)[:1000]}")
        return 0
    finally:
        conn.close()


def cmd_config_show(args: argparse.Namespace) -> int:
    import json as _json

    settings = _load_settings(args)
    path = resolve_config_file(getattr(args, "config", None))
    print(
        _json.dumps(
            {
                "file": str(path) if path is not None else "",
                "fingerprint": settings_fingerprint(settings),
                "settings": redacted_dict(settings),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from libref.mcp_server import build_mcp_server, check_startup

    settings = _load_settings(args, _mcp_tools_overrides(args))
    if args.http:
        host = args.host or settings.mcp_host
        try:
            check_startup(
                host,
                require_auth=bool(settings.mcp_require_auth),
                token=settings.mcp_token,
            )
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    # MCPServer runs tools in a worker thread; the connection must be reusable across threads.
    conn = _connect(settings, check_same_thread=False)
    mcp = build_mcp_server(conn, settings)
    if args.http:
        host = args.host or settings.mcp_host
        port = args.port or settings.mcp_port
        path = args.path or settings.mcp_path
        mcp.run(
            "streamable-http", host=host, port=port, streamable_http_path=path
        )  # blocks until the process exits
    else:
        mcp.run("stdio")  # blocks until stdio closes
    conn.close()
    return 0


def cmd_serve_web(args: argparse.Namespace) -> int:
    from libref.mcp_server.auth import check_startup

    settings = _load_settings(args)
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    try:
        check_startup(
            host,
            require_auth=bool(settings.mcp_require_auth),
            token=settings.mcp_token,
        )
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    from libref.web import build_web_app

    conn = _connect(settings, check_same_thread=False)
    from libref.services.factory import build_gateway

    gateway = build_gateway(settings)
    vec_store, embed = _build_vector(settings, gateway)
    app = build_web_app(
        conn,
        settings,
        vec_store=vec_store,
        embed=embed,
        rerank=gateway.rerank_fn(),
        chat=gateway.chat_fn(),
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
    conn.close()
    return 0
