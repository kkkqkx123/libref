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


def _remote_job_show(args: argparse.Namespace, settings) -> int:
    import json as _json2

    import httpx as _httpx2

    from libref.cli.remote import auth_headers as _ah2
    from libref.cli.remote import base_url_for as _buf2

    base = _buf2(args, settings)
    try:
        with _httpx2.Client(timeout=30.0) as _c:
            if getattr(args, "job_id", None) is not None and not getattr(
                args, "list", False
            ):
                _r = _c.get(f"{base}/jobs/{int(args.job_id)}", headers=_ah2(settings))
                _r.raise_for_status()
                out = _r.json()
                if "error" in out and "id" not in out:
                    import sys as _sys

                    print(f"(unknown job: {args.job_id})", file=_sys.stderr)
                    return 1
                print(
                    f"job {out.get('id')} {out.get('status')}"
                    + (f" error={out.get('error')}" if out.get("error") else "")
                )
                if out.get("result"):
                    print(
                        f"result: {_json2.dumps(out.get('result'), ensure_ascii=False)[:2000]}"
                    )
                return 0
            _r = _c.get(
                f"{base}/jobs",
                params={"limit": int(getattr(args, "limit", 20) or 20)},
                headers=_ah2(settings),
            )
            _r.raise_for_status()
            out = _r.json()
            for job in out.get("jobs") or []:
                print(
                    f"{job.get('id')} {job.get('kind')} {job.get('status')} {job.get('finished_at') or ''}"
                )
            return 0
    except Exception as exc:  # noqa: BLE001
        import sys as _sys2

        print(f"remote failed: {exc}", file=_sys2.stderr)
        return 1


def cmd_job_show(args: argparse.Namespace) -> int:
    import json as _json

    _st2 = _load_settings(args)
    from libref.cli.remote import is_remote as _is_remote2

    if _is_remote2(args, _st2):
        return _remote_job_show(args, _st2)
    conn = _connect(_st2)
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


def _startup_auth_state(settings, conn) -> bool:
    """Whether identity rows already exist for enforced boot checks."""
    try:
        row = conn.execute("SELECT id FROM principals LIMIT 1").fetchone()
        return row is not None
    except Exception:  # noqa: BLE001 — missing tables count as no principals
        return False


def _ensure_bootstrap(settings, conn) -> None:
    """Idempotently create the platform administrator from the bootstrap key."""
    if str(getattr(settings, "auth_mode", "dev") or "dev") != "enforced":
        return
    raw = getattr(settings, "bootstrap_admin_key", None)
    if _startup_auth_state(settings, conn):
        return
    if not raw:
        return
    from libref.services.infra.auth import bootstrap_admin

    bootstrap_admin(conn, str(raw))


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from libref.mcp_server import build_mcp_server
    from libref.mcp_server.auth import check_startup

    settings = _load_settings(args, _mcp_tools_overrides(args))
    conn = _connect(settings, check_same_thread=False)
    try:
        _ensure_bootstrap(settings, conn)
        try:
            check_startup(
                args.host or settings.mcp_host,
                auth_mode=str(getattr(settings, "auth_mode", "dev") or "dev"),
                bootstrap_key=getattr(settings, "bootstrap_admin_key", None),
                has_principals=_startup_auth_state(settings, conn),
            )
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    except Exception:  # noqa: BLE001, S110
        pass
    mcp = build_mcp_server(conn, settings)
    if args.http:
        host = args.host or settings.mcp_host
        port = args.port or settings.mcp_port
        path = args.path or settings.mcp_path
        mcp.run("streamable-http", host=host, port=port, streamable_http_path=path)
    else:
        mcp.run("stdio")
    conn.close()
    return 0


def cmd_serve_web(args: argparse.Namespace) -> int:
    from libref.mcp_server.auth import check_startup

    settings = _load_settings(args)
    conn = _connect(settings, check_same_thread=False)
    try:
        _ensure_bootstrap(settings, conn)
        try:
            check_startup(
                args.host or settings.web_host,
                auth_mode=str(getattr(settings, "auth_mode", "dev") or "dev"),
                bootstrap_key=getattr(settings, "bootstrap_admin_key", None),
                has_principals=_startup_auth_state(settings, conn),
            )
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    except Exception:  # noqa: BLE001, S110
        pass
    import uvicorn

    from libref.services.factory import build_gateway
    from libref.web import build_web_app

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
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    uvicorn.run(app, host=host, port=port, log_level="warning")
    conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Single combined serving: Web routes plus MCP mounted at the same port."""
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount

    from libref.mcp_server.auth import check_startup
    from libref.services.infra.context import build_context
    from libref.web.middleware import AuthMiddleware

    settings = _load_settings(args, _mcp_tools_overrides(args))
    host = getattr(args, "host", None) or settings.web_host
    port = getattr(args, "port", None) or settings.web_port
    conn = _connect(settings, check_same_thread=False)
    try:
        _ensure_bootstrap(settings, conn)
        try:
            check_startup(
                host,
                auth_mode=str(getattr(settings, "auth_mode", "dev") or "dev"),
                bootstrap_key=getattr(settings, "bootstrap_admin_key", None),
                has_principals=_startup_auth_state(settings, conn),
            )
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        ctx = build_context(settings)
        if getattr(args, "stdio", False):
            from libref.mcp_server import build_mcp_server as _build_mcp

            mcp = _build_mcp(conn, settings, context=ctx)
            mcp.run("stdio")
            return 0
        from libref.mcp_server import build_mcp_server as _build_mcp
        from libref.web import build_web_app as _build_web

        gateway = ctx.gateway
        web_app = _build_web(
            conn,
            settings,
            vec_store=ctx.vec_store,
            embed=ctx.embed,
            rerank=gateway.rerank_fn() if gateway else None,
            chat=gateway.chat_fn() if gateway else None,
            context=ctx,
        )
        mcp = _build_mcp(conn, settings, context=ctx)
        transport_kwargs: dict = {}
        allowed_hosts = list(getattr(settings, "allowed_hosts", None) or [])
        allowed_origins = list(getattr(settings, "allowed_origins", None) or [])
        if allowed_hosts or allowed_origins:
            from mcp.server.transport_security import TransportSecuritySettings

            transport_kwargs["transport_security"] = TransportSecuritySettings(
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            )
        transport_kwargs["host"] = host
        mcp_path = str(getattr(settings, "mcp_path", "/mcp") or "/mcp")
        mcp_starlette = mcp.streamable_http_app(
            streamable_http_path=mcp_path, **transport_kwargs
        )
        routes = list(getattr(web_app, "routes", []))
        routes.append(Mount("/", app=mcp_starlette))
        from contextlib import asynccontextmanager as _acm

        @_acm
        async def _lifespan(_app):
            async with mcp.session_manager.run():
                worker_conn = ctx.connect(check_same_thread=False)
                try:
                    from libref.services.docs.scrape_jobs import build_scrape_runner
                    from libref.services.infra.jobs import JobManager

                    mgr = JobManager(worker_conn, instance_id=str(settings.instance_id))
                    mgr.recover(requeue=True)
                    runner = build_scrape_runner(
                        settings,
                        worker_conn,
                        vec_store=ctx.vec_store,
                        embed=ctx.embed,
                        mgr=mgr,
                    )
                    mgr.start_worker(runner)
                    try:
                        yield
                    finally:
                        mgr.stop_worker()
                finally:
                    try:
                        worker_conn.close()
                    except Exception:  # noqa: BLE001, S110 — best effort
                        pass

        from starlette.applications import Starlette as _Starlette

        origins = allowed_origins or ["*"]
        combined = _Starlette(routes=routes, lifespan=_lifespan)
        combined.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Last-Event-ID",
                "Mcp-*",
                "*",
            ],
            expose_headers=["Mcp-Session-Id", "x-request-id"],
        )
        combined.add_middleware(AuthMiddleware, context=ctx)
        uvicorn.run(combined, host=host, port=port, log_level="warning")
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 — best effort
            pass


def _remote_job_cancel(args: argparse.Namespace, settings) -> int:
    import httpx as _httpx3

    from libref.cli.remote import auth_headers as _ah3
    from libref.cli.remote import base_url_for as _buf3

    base = _buf3(args, settings)
    try:
        with _httpx3.Client(timeout=30.0) as _c:
            _r = _c.post(
                f"{base}/jobs/{int(args.job_id)}/cancel", headers=_ah3(settings)
            )
            _r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        import sys as _sys3

        print(f"remote failed: {exc}", file=_sys3.stderr)
        return 1
    print("ok")
    return 0


def cmd_job_cancel(args: argparse.Namespace) -> int:
    import sys as _sys4

    _st3 = _load_settings(args)
    from libref.cli.remote import is_remote as _is_remote3

    if _is_remote3(args, _st3):
        return _remote_job_cancel(args, _st3)
    from libref.services.infra.jobs import JobManager as _JM

    conn = _connect(_st3)
    try:
        try:
            _JM(conn).cancel(int(args.job_id))
        except ValueError:
            print(f"(unknown job: {args.job_id})", file=_sys4.stderr)
            return 1
    finally:
        conn.close()
    print("ok")
    return 0
