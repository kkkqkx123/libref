"""Server-local principal and API key administration."""

from __future__ import annotations

import argparse
import json
from typing import Any

from libref.cli.common import _connect, _load_settings
from libref.errors import invalid_argument
from libref.services.infra.timestamps import epoch_utc


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _audit(conn, tenant: str, action: str, target: str, *, result: str = "ok") -> None:
    from libref.services.ops.audit import write_audit

    write_audit(
        conn,
        tenant_id=tenant,
        principal_id=None,
        action=action,
        target=target,
        result=result,
    )


def _tenant(args: argparse.Namespace) -> str:
    return str(args.tenant or "default")


def _expiry(text: str | None) -> float | None:
    if text is None:
        return None
    value = epoch_utc(text)
    if value is None:
        raise invalid_argument(
            "expires_at must be an ISO 8601 instant", field="expires_at"
        )
    return value


def _tenant_for_key(conn, key_id: int) -> str | None:
    row = conn.execute(
        "SELECT p.tenant_id FROM api_keys k JOIN principals p ON p.id=k.principal_id"
        " WHERE k.id=?",
        (key_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def cmd_principal_list(args: argparse.Namespace) -> int:
    from libref.services.infra.auth import list_principals, system_caller

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        rows = list_principals(
            conn, viewer=system_caller(), tenant_id=getattr(args, "tenant", None)
        )
        _audit(conn, _tenant(args), "cli.principals.list", str(len(rows)))
    finally:
        conn.close()
    if getattr(args, "json", False):
        _print_json(rows)
    else:
        for row in rows:
            print(
                f"{row['id']} {row['tenant_id']} {row['kind']} {row['name']} "
                f"roles={','.join(row['roles'])}"
            )
    return 0


def cmd_key_list(args: argparse.Namespace) -> int:
    from libref.services.infra.auth import list_api_keys, system_caller

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        rows = list_api_keys(
            conn, viewer=system_caller(), tenant_id=getattr(args, "tenant", None)
        )
        _audit(conn, _tenant(args), "cli.keys.list", str(len(rows)))
    finally:
        conn.close()
    if getattr(args, "json", False):
        _print_json(rows)
    else:
        for row in rows:
            print(
                f"{row['id']} principal={row['principal_id']} "
                f"prefix={row['key_prefix']} scopes={','.join(row['scopes'])}"
            )
    return 0


def cmd_key_issue(args: argparse.Namespace) -> int:
    from libref.services.infra.auth import (
        create_principal,
        issue_api_key,
        system_caller,
        validate_key_scopes,
    )

    caller = system_caller()
    roles = list(args.role or [])
    scopes = list(args.scope or [])
    validate_key_scopes(scopes, caller, target_roles=roles)
    expires_at = _expiry(getattr(args, "expires_at", None))
    tenant = _tenant(args)
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        principal_id = create_principal(
            conn,
            caller=caller,
            tenant_id=tenant,
            kind=args.kind,
            name=args.name,
            roles=roles,
        )
        key_id, raw = issue_api_key(
            conn,
            caller=caller,
            settings=settings,
            principal_id=principal_id,
            scopes=scopes,
            expires_at=expires_at,
        )
        _audit(conn, tenant, "cli.keys.issue", str(key_id))
    except ValueError:
        _audit(conn, tenant, "cli.keys.issue", args.name, result="denied")
        raise
    finally:
        conn.close()
    if getattr(args, "json", False):
        _print_json({"id": key_id, "principal_id": principal_id, "key": raw})
    else:
        print(f"{key_id} {raw}")
    return 0


def cmd_key_attach(args: argparse.Namespace) -> int:
    from libref.services.infra.auth import (
        issue_api_key,
        principal_tenant,
        system_caller,
    )

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        tenant = principal_tenant(conn, args.principal_id)
        if tenant is None:
            raise invalid_argument(
                f"unknown principal: {args.principal_id}", field="principal_id"
            )
        key_id, raw = issue_api_key(
            conn,
            caller=system_caller(),
            settings=settings,
            principal_id=args.principal_id,
            scopes=list(args.scope or []),
            expires_at=_expiry(getattr(args, "expires_at", None)),
        )
        _audit(conn, tenant, "cli.keys.issue", str(key_id))
    except ValueError:
        _audit(
            conn,
            tenant or "default",
            "cli.keys.issue",
            str(args.principal_id),
            result="denied",
        )
        raise
    finally:
        conn.close()
    if getattr(args, "json", False):
        _print_json({"id": key_id, "principal_id": args.principal_id, "key": raw})
    else:
        print(f"{key_id} {raw}")
    return 0


def cmd_key_revoke(args: argparse.Namespace) -> int:
    from libref.services.infra.auth import revoke_api_key, system_caller

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        tenant = _tenant_for_key(conn, args.key_id)
        if tenant is None:
            raise invalid_argument(f"unknown api key: {args.key_id}", field="key_id")
        try:
            revoke_api_key(conn, caller=system_caller(), key_id=args.key_id)
        except ValueError:
            _audit(conn, tenant, "cli.keys.revoke", str(args.key_id), result="denied")
            raise
        _audit(conn, tenant, "cli.keys.revoke", str(args.key_id))
    finally:
        conn.close()
    print("ok")
    return 0


def cmd_key_revoke_all(args: argparse.Namespace) -> int:
    from libref.services.infra.auth import (
        principal_tenant,
        revoke_principal_keys,
        system_caller,
    )

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        tenant = principal_tenant(conn, args.principal_id)
        if tenant is None:
            raise invalid_argument(
                f"unknown principal: {args.principal_id}", field="principal_id"
            )
        count = revoke_principal_keys(
            conn, caller=system_caller(), principal_id=args.principal_id
        )
        _audit(conn, tenant, "cli.keys.revoke_all", str(args.principal_id))
    finally:
        conn.close()
    print(f"revoked: {count}")
    return 0
