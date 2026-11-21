"""Operations commands: database, vectors, jobs, retention, serving."""

from __future__ import annotations

import argparse
import logging
import sys

from libref.cli.common import (
    _build_vector,
    _connect,
    _load_settings,
    _mcp_tools_overrides,
    applied_count,
    as_error,
)
from libref.config import redacted_dict, resolve_config_file, settings_fingerprint
from libref.errors import LibrefError, invalid_argument, unavailable
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
            raise unavailable("embedding is not configured (missing key)")
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
            raise unavailable("embedding is not configured (missing key)")
        vids = mining_version_ids(conn, args.library, args.version, args.ecosystem)
        if len(vids) != 1:
            raise invalid_argument("library version must resolve to a single version")
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


def cmd_vectors_status(args: argparse.Namespace) -> int:
    import json as _json

    from libref.services.infra.vectors import build_store
    from libref.store.vec_store import close_simvec_engines

    settings = _load_settings(args)
    try:
        info = build_store(settings).diagnostics()
    finally:
        close_simvec_engines()
    print(_json.dumps(info, ensure_ascii=False, indent=2, sort_keys=True))
    # Reachability is the health signal; a missing collection is a first-run state,
    # not a failure, so the command reports it without failing the probe.
    return 0 if info.get("reachable") else 1


def cmd_vectors_optimize(args: argparse.Namespace) -> int:
    import json as _json

    from libref.services.infra.vectors import build_store
    from libref.store.vec_store import close_simvec_engines

    settings = _load_settings(args)
    try:
        out = build_store(settings).optimize(wait=True)
    finally:
        close_simvec_engines()
    print(_json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


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
                raise LibrefError("review_conflict", text) from exc
            raise as_error(exc) from exc
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
    except Exception as exc:
        raise as_error(exc) from exc


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
        raise as_error(exc) from exc
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


def cmd_role_audit(args: argparse.Namespace) -> int:
    """Report principals whose role combination breaks separation of duties."""
    import json as _json

    from libref.services.infra.auth import scan_role_conflicts, separation_advice

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        conflicts = scan_role_conflicts(conn)
    finally:
        conn.close()
    if getattr(args, "json", False):
        print(_json.dumps(conflicts, ensure_ascii=False, indent=2))
        return 1 if conflicts and getattr(args, "strict", False) else 0
    if not conflicts:
        print("(no role conflicts)")
        return 0
    for row in conflicts:
        groups = " | ".join("+".join(group) for group in row["groups"])
        print(
            f"{row['principal_id']} {row['tenant_id']}/{row['name']}"
            f" roles={','.join(row['roles'])} conflicting={groups}"
        )
        for plan in separation_advice(row["roles"]):
            print(
                f"    split: --principal {row['principal_id']}"
                f" --keep {plan['keep']}  (moves {','.join(plan['move'])})"
            )
    print(
        f"total: {len(conflicts)} principal(s); split the roles across separate"
        " principals to restore separation of duties"
    )
    return 1 if getattr(args, "strict", False) else 0


def cmd_role_split(args: argparse.Namespace) -> int:
    """Split one conflicting principal into a kept role plus new principals."""
    import json as _json

    from libref.services.infra.auth import dev_principal, split_role_conflict
    from libref.services.ops.audit import write_audit

    settings = _load_settings(args)
    conn = _connect(settings)
    caller = dev_principal(settings)
    try:
        result = split_role_conflict(
            conn,
            caller=caller,
            settings=settings,
            principal_id=int(args.principal),
            keep_role=str(args.keep),
            new_name=getattr(args, "name", None),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        if not result["dry_run"]:
            write_audit(
                conn,
                tenant_id=result["tenant_id"],
                principal_id=None,
                action="roles.split",
                target=f"{result['principal_id']}->{len(result['new_principals'])}",
            )
    except ValueError as exc:
        raise as_error(exc) from exc
    finally:
        conn.close()
    if getattr(args, "json", False):
        print(_json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    kept = ",".join(result["kept_roles"])
    moved = ",".join(result["moved_roles"])
    if result["dry_run"]:
        print(
            f"dry run: principal {result['principal_id']} keeps {kept}; moves {moved}"
        )
        for index, chunk in enumerate(result["planned"]):
            print(f"  would create principal #{index + 1} roles={','.join(chunk)}")
        print("no changes written")
        return 0
    print(f"principal {result['principal_id']} keeps {kept}; moved {moved}")
    for part in result["new_principals"]:
        print(
            f"  principal {part['principal_id']} {part['name']}"
            f" roles={','.join(part['roles'])}"
        )
        print(f"    key {part['key_id']}: {part['key']}")
    print("store these keys now: the plaintext is not retrievable later")
    return 0


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
    if str(getattr(settings, "auth_mode", "enforced") or "enforced") != "enforced":
        return
    raw = getattr(settings, "bootstrap_admin_key", None)
    if _startup_auth_state(settings, conn) or not raw:
        return
    from libref.services.infra.auth import bootstrap_admin

    bootstrap_admin(conn, str(raw), settings)


def _warn_role_conflicts(conn) -> None:
    """Report stale role combinations without blocking the boot.

    Blocking would turn an existing database into an outage on upgrade; the
    combination is a compliance defect, not an unsafe runtime state.
    """
    from libref.services.infra.auth import scan_role_conflicts

    try:
        conflicts = scan_role_conflicts(conn)
    except Exception:  # noqa: BLE001 — advisory check must never block startup
        return
    if not conflicts:
        return
    print(
        f"security warning: {len(conflicts)} principal(s) break separation of duties;"
        " run `libref role-audit` for details",
        file=sys.stderr,
    )


def _prepare_auth(host: str, settings, conn) -> None:
    """Bootstrap identity and validate startup; raise on unsafe configuration."""
    from libref.services.infra.auth import check_startup

    try:
        _ensure_bootstrap(settings, conn)
        check_startup(
            host,
            auth_mode=str(getattr(settings, "auth_mode", "enforced") or "enforced"),
            bootstrap_key=getattr(settings, "bootstrap_admin_key", None),
            has_principals=_startup_auth_state(settings, conn),
        )
    except Exception as exc:
        raise LibrefError(
            "config_refresh_failed", f"startup check failed: {exc}"
        ) from exc
    _warn_role_conflicts(conn)


def _use_local_principal(settings) -> None:
    """Grant the trusted stdio operator a local platform principal."""
    from libref.services.infra.auth import current_principal, dev_principal

    current_principal.set(dev_principal(settings))


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    """Serve MCP over stdio; HTTP MCP is always the combined serve command."""
    from libref.mcp_server import build_mcp_server

    settings = _load_settings(args, _mcp_tools_overrides(args))
    conn = _connect(settings, check_same_thread=False)
    _use_local_principal(settings)
    mcp = build_mcp_server(conn, settings)
    _verify_fingerprint(settings, conn)
    mcp.run("stdio")
    _drain_and_close(settings, conn)
    return 0


def _verify_fingerprint(settings, conn) -> None:
    """Refuse to serve when this replica's settings diverge from the fleet."""
    from libref.services.infra.runtime import check_config_fingerprint

    if not check_config_fingerprint(conn, settings):
        raise LibrefError(
            "config_refresh_failed",
            "settings fingerprint diverges from the shared control plane; "
            "refusing to serve",
        )


def _drain_and_close(settings, conn) -> None:
    """Graceful exit for serving commands: drain in-flight jobs, release storage."""
    from libref.services.infra.jobs import JobManager
    from libref.services.infra.runtime import drain

    try:
        drain(JobManager(conn), timeout=float(getattr(settings, "drain_timeout", 30.0)))
    except Exception:
        logging.getLogger(__name__).debug("drain on shutdown failed", exc_info=True)
    conn.close()


def cmd_serve(args: argparse.Namespace) -> int:
    """Single combined serving: Web routes plus MCP mounted at the same port."""
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware
    from starlette.routing import Mount

    from libref.services.infra.context import build_context
    from libref.web.middleware import AuthMiddleware

    settings = _load_settings(args, _mcp_tools_overrides(args))
    host = getattr(args, "host", None) or settings.web_host
    port = getattr(args, "port", None) or settings.web_port
    conn = _connect(settings, check_same_thread=False)
    try:
        _prepare_auth(host, settings, conn)
        ctx = build_context(settings)
        if getattr(args, "stdio", False):
            from libref.mcp_server import build_mcp_server as _build_mcp

            _use_local_principal(settings)
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

            from libref.services.infra.auth import is_loopback_host

            hosts = list(allowed_hosts)
            if not hosts and is_loopback_host(host):
                hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
            if not hosts:
                hosts = [f"{host}:*"]
            transport_kwargs["transport_security"] = TransportSecuritySettings(
                allowed_hosts=hosts,
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
                    from libref.services.code.code_ingest import (
                        INDEX_REPO_JOB_KIND,
                        build_repo_runner,
                    )
                    from libref.services.code.mining_refresh import (
                        INDUCE_LABELS_JOB_KIND,
                        MINE_PATTERNS_JOB_KIND,
                        build_induce_runner,
                        build_mine_runner,
                    )
                    from libref.services.docs.scrape_jobs import (
                        SCRAPE_JOB_KIND,
                        build_scrape_runner,
                    )
                    from libref.services.infra.jobs import JobManager
                    from libref.services.ops.index_requests import (
                        build_index_request_runner,
                    )

                    mgr = JobManager(worker_conn, instance_id=str(settings.instance_id))
                    mgr.recover(requeue=True)
                    scrape_runner = build_scrape_runner(
                        settings,
                        worker_conn,
                        vec_store=ctx.vec_store,
                        embed=ctx.embed,
                        mgr=mgr,
                    )
                    repo_runner = build_repo_runner(
                        settings,
                        worker_conn,
                        vec_store=ctx.vec_store,
                        embed=ctx.embed,
                        mgr=mgr,
                    )
                    mine_runner = build_mine_runner(settings, worker_conn)
                    induce_runner = build_induce_runner(settings, worker_conn)

                    async def dispatch(job):
                        if job.kind == SCRAPE_JOB_KIND:
                            return await scrape_runner(job)
                        if job.kind == INDEX_REPO_JOB_KIND:
                            return await repo_runner(job)
                        if job.kind == MINE_PATTERNS_JOB_KIND:
                            return await mine_runner(job)
                        if job.kind == INDUCE_LABELS_JOB_KIND:
                            return await induce_runner(job)
                        raise ValueError(f"unsupported job kind: {job.kind}")

                    mgr.start_worker(
                        build_index_request_runner(
                            settings, worker_conn, dispatch, jobs=mgr
                        )
                    )
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

        combined = _Starlette(routes=routes, lifespan=_lifespan)
        combined.state.context = ctx
        combined.state.settings = settings
        combined.add_middleware(AuthMiddleware, context=ctx)
        # CORS wraps Auth so browser preflight receives allowed-origin headers.
        combined.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
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
        # Flattened web routes bypass the inner app stack, so the combined app
        # must restore the FastAPI route invariant and the SPA fallback itself.
        from fastapi.middleware.asyncexitstack import AsyncExitStackMiddleware

        combined.add_middleware(AsyncExitStackMiddleware)
        from libref.web.spa import SpaMiddleware, spa_dir_for

        spa_directory = spa_dir_for(settings)
        if spa_directory is not None:
            combined.add_middleware(
                SpaMiddleware,
                directory=spa_directory,
                mcp_prefix=str(getattr(settings, "mcp_path", "/mcp") or "/mcp"),
                api_paths={
                    str(getattr(route, "path", ""))
                    for route in routes
                    if getattr(route, "path", "")
                },
            )
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
    except Exception as exc:
        raise as_error(exc) from exc
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
