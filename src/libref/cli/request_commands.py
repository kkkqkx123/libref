"""Index-request CLI: thin exit over the shared request service or HTTP API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from libref.cli.common import _build_vector, _connect, _load_settings
from libref.cli.remote import base_url_for, call_json
from libref.services.infra.auth import Principal, dev_principal, lookup_principal

_TERMINAL = frozenset({"done", "failed", "rejected", "cancelled"})


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _error(exc: Exception) -> int:
    from libref.services.ops.index_requests import IndexRequestError

    if isinstance(exc, IndexRequestError):
        return _fail(f"{exc.code}: {exc.message}")
    return _fail(f"failed: {exc}")


def _local_principal(conn: Any, settings: Any) -> Principal:
    from libref.services.infra.auth import current_principal

    found = current_principal.get()
    if found is not None:
        return found
    raw = str(getattr(settings, "client_api_key", "") or "")
    if raw:
        principal = lookup_principal(conn, raw)
        if principal is None:
            raise ValueError("client_api_key does not match an active key")
        return principal
    if str(getattr(settings, "auth_mode", "enforced") or "enforced") == "dev":
        return dev_principal(settings)
    raise ValueError("local requests require --remote with a key, or dev auth mode")


def _service(args: argparse.Namespace, settings: Any, conn: Any):
    from libref.services.infra.jobs import JobManager
    from libref.services.ops.index_requests import IndexRequestService

    jobs = JobManager(
        conn, stale_after=float(getattr(settings, "heartbeat_timeout", 600))
    )
    return IndexRequestService(conn, settings, jobs=jobs), jobs


def _tenant(principal: Principal) -> str | None:
    from libref.services.infra.auth import PLATFORM_SCOPE

    return "*" if PLATFORM_SCOPE in principal.scopes else principal.tenant_id


def _payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": args.kind,
        "library": args.library,
        "version": args.version or "",
    }
    optional = {
        "ecosystem": args.ecosystem,
        "source": args.source,
        "start_url": args.start_url,
        "max_pages": args.max_pages,
        "prefilter": args.prefilter,
        "update": args.update,
        "fresh": args.fresh,
        "include_prerelease": args.include_prerelease,
        "subdir": args.subdir,
        "only_files": args.only_file,
        "exclude_patterns": args.exclude_pattern,
        "max_files": args.max_files,
    }
    payload.update(
        {
            key: value
            for key, value in optional.items()
            if value not in (None, False, [])
        }
    )
    if getattr(args, "scope", None):
        try:
            payload["scope"] = json.loads(args.scope)
        except ValueError as exc:
            raise ValueError(f"--scope must be a JSON object: {exc}") from exc
    return payload


def _remote(
    args: argparse.Namespace,
    settings: Any,
    method: str,
    path: str,
    body: dict | None = None,
) -> Any:
    return call_json(method, f"{base_url_for(args, settings)}{path}", settings, body)


async def _drive_local_wait(
    args, settings, conn, svc, jobs, request_id, principal
) -> dict:
    from libref.services.code.code_ingest import INDEX_REPO_JOB_KIND, build_repo_runner
    from libref.services.docs.scrape_jobs import SCRAPE_JOB_KIND, build_scrape_runner
    from libref.services.ops.index_requests import build_index_request_runner

    item = svc.get(request_id, principal, tenant_id=_tenant(principal))
    if item["job_id"] is None or item["status"] in _TERMINAL:
        return await svc.wait(
            request_id,
            principal,
            tenant_id=_tenant(principal),
            timeout=float(args.timeout),
        )
    vec_store, embed = _build_vector(settings)

    async def dispatch(job):
        if job.kind == SCRAPE_JOB_KIND:
            return await build_scrape_runner(
                settings, conn, vec_store=vec_store, embed=embed, mgr=jobs
            )(job)
        if job.kind == INDEX_REPO_JOB_KIND:
            return await build_repo_runner(
                settings, conn, vec_store=vec_store, embed=embed, mgr=jobs
            )(job)
        raise ValueError(f"unsupported job kind: {job.kind}")

    jobs.start_worker(build_index_request_runner(settings, conn, dispatch, jobs=jobs))
    try:
        return await svc.wait(
            request_id,
            principal,
            tenant_id=_tenant(principal),
            timeout=float(args.timeout),
        )
    finally:
        jobs.stop_worker()


def cmd_request(args: argparse.Namespace) -> int:
    from libref.cli.remote import is_remote

    settings = _load_settings(args)
    action = args.request_action
    remote = is_remote(args, settings)
    try:
        if remote:
            if action == "submit":
                item = _remote(
                    args, settings, "POST", "/index-requests", _payload(args)
                )
            elif action == "list":
                params = f"?limit={int(args.limit)}"
                if args.status:
                    params += f"&status={args.status}"
                if args.pending:
                    item = _remote(
                        args, settings, "GET", f"/index-requests/pending{params}"
                    )
                else:
                    item = _remote(args, settings, "GET", f"/index-requests{params}")
            elif action == "show":
                item = _remote(
                    args, settings, "GET", f"/index-requests/{int(args.request_id)}"
                )
            elif action == "approve":
                item = _remote(
                    args,
                    settings,
                    "POST",
                    f"/index-requests/{int(args.request_id)}/approve",
                )
            elif action == "reject":
                item = _remote(
                    args,
                    settings,
                    "POST",
                    f"/index-requests/{int(args.request_id)}/reject",
                    {"reason": args.reason},
                )
            elif action == "cancel":
                item = _remote(
                    args,
                    settings,
                    "POST",
                    f"/index-requests/{int(args.request_id)}/cancel",
                )
            else:
                deadline = time.monotonic() + float(args.timeout)
                while True:
                    item = _remote(
                        args, settings, "GET", f"/index-requests/{int(args.request_id)}"
                    )
                    if item["status"] in _TERMINAL or time.monotonic() >= deadline:
                        break
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        else:
            conn = _connect(settings)
            try:
                principal = _local_principal(conn, settings)
                svc, jobs = _service(args, settings, conn)
                if action == "submit":
                    item = asyncio.run(
                        svc.submit(
                            _payload(args),
                            principal,
                            tenant_id=_tenant(principal),
                        )
                    )
                elif action == "list":
                    item = {
                        "items": svc.list(
                            principal,
                            tenant_id=_tenant(principal),
                            status=args.status,
                            pending=bool(args.pending),
                            limit=int(args.limit),
                        )
                    }
                elif action == "show":
                    item = svc.get(
                        int(args.request_id), principal, tenant_id=_tenant(principal)
                    )
                elif action == "approve":
                    item = svc.approve(
                        int(args.request_id), principal, tenant_id=_tenant(principal)
                    )
                elif action == "reject":
                    item = svc.reject(
                        int(args.request_id),
                        args.reason,
                        principal,
                        tenant_id=_tenant(principal),
                    )
                elif action == "cancel":
                    item = svc.cancel(
                        int(args.request_id), principal, tenant_id=_tenant(principal)
                    )
                else:
                    item = asyncio.run(
                        _drive_local_wait(
                            args,
                            settings,
                            conn,
                            svc,
                            jobs,
                            int(args.request_id),
                            principal,
                        )
                    )
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001 — CLI surfaces stable messages, not tracebacks
        return _error(exc)

    if action == "list":
        for row in item.get("items", []):
            print(f"{row['id']}\t{row['status']}\t{row['kind']}\t{row['target']}")
        return 0
    if action == "wait":
        _print(item)
        return 0 if item["status"] == "done" else 1
    _print(item)
    return 0
