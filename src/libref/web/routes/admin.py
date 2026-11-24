"""Web routes for admin."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request

from libref.config import settings_fingerprint
from libref.services.infra.auth import (
    PLATFORM_SCOPE,
)
from libref.services.infra.timestamps import epoch_utc
from libref.web.api import (
    ApiKeyResponse,
    ApiValidationError,
    ConfigRefreshResponse,
    ErrorResponse,
    IssuedKey,
    KeyAttach,
    KeyCreate,
    OkResponse,
    PrincipalListResponse,
    QuotaPolicyResponse,
    QuotaPolicyUpsert,
    RevokedKeys,
    envelope,
)
from libref.web.errors import ApiRejection
from libref.web.request_context import (
    _guard,
    _principal_id,
    _tenant_of,
    _write_tenant_of,
)


def register(app, ctx) -> None:
    conn = ctx.conn
    base_openapi = app.openapi
    settings = ctx.settings
    _need = ctx.need
    _eff_settings = ctx.eff_settings
    _req_bundle = ctx.req_bundle

    @app.get("/admin/principals", response_model=PrincipalListResponse)
    def admin_principals_list(
        request: Request,
        tenant_id: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        """Read-only principal view; role conflicts are flagged, never repaired."""
        _need(principal, "admin:keys")
        from libref.services.infra.auth import list_principals
        from libref.services.ops.audit import write_audit

        rows = list_principals(conn, viewer=principal, tenant_id=tenant_id)
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="principals.list",
            target=str(len(rows)),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return envelope([dict(row) for row in rows])

    @app.get("/admin/keys", response_model=ApiKeyResponse)
    def admin_keys_list(
        request: Request,
        tenant_id: str | None = None,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import list_api_keys
        from libref.services.ops.audit import write_audit

        items = list_api_keys(conn, viewer=principal, tenant_id=tenant_id)
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.list",
            target=str(len(items)),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return envelope([dict(row) for row in items])

    @app.post("/admin/keys", response_model=IssuedKey)
    def admin_keys_create(
        body: KeyCreate, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import (
            PLATFORM_ROLE,
            create_principal,
            issue_api_key,
        )
        from libref.services.ops.audit import write_audit

        try:
            pid = create_principal(
                conn,
                caller=principal,
                tenant_id=body.tenant_id,
                kind=body.kind,
                name=body.name,
                roles=list(body.roles),
            )
            key_id, raw = issue_api_key(
                conn,
                caller=principal,
                settings=_eff_settings(request),
                principal_id=pid,
                scopes=list(body.scopes),
                expires_at=epoch_utc(body.expires_at),
            )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        # The platform role is exempt from separation of duties, so granting it
        # deserves its own audit action rather than blending in with tenant keys.
        action = (
            "keys.issue.platform" if PLATFORM_ROLE in list(body.roles) else "keys.issue"
        )
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action=action,
            target=str(key_id),
            request_id=str(getattr(getattr(request, "state", None), "request_id", "")),
        )
        return {"id": key_id, "key": raw, "prefix": raw[:12]}

    @app.post("/admin/principals/{principal_id}/keys", response_model=IssuedKey)
    def admin_principal_key_create(
        principal_id: int,
        body: KeyAttach,
        request: Request,
        principal=Depends(_guard),
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import issue_api_key, principal_tenant
        from libref.services.ops.audit import write_audit

        tenant = principal_tenant(conn, principal_id)
        if tenant is None:
            raise ApiRejection(404, "not_found", f"unknown principal: {principal_id}")
        try:
            key_id, raw = issue_api_key(
                conn,
                caller=principal,
                settings=_eff_settings(request),
                principal_id=principal_id,
                scopes=list(body.scopes),
                expires_at=epoch_utc(body.expires_at),
            )
        except ValueError as exc:
            raise ApiRejection(400, "invalid_argument", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=tenant,
            principal_id=_principal_id(principal),
            action="keys.issue",
            target=str(key_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"id": key_id, "key": raw, "prefix": raw[:12]}

    @app.delete("/admin/keys/{key_id}", response_model=OkResponse)
    def admin_keys_revoke(
        key_id: int, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import revoke_api_key
        from libref.services.ops.audit import write_audit

        try:
            revoke_api_key(conn, caller=principal, key_id=key_id)
        except ValueError as exc:
            write_audit(
                conn,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="keys.revoke",
                target=str(key_id),
                result="denied",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke",
            target=str(key_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True}

    @app.delete("/admin/principals/{principal_id}/keys", response_model=RevokedKeys)
    def admin_principal_keys_revoke(
        principal_id: int, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:keys")
        from libref.services.infra.auth import revoke_principal_keys
        from libref.services.ops.audit import write_audit

        try:
            count = revoke_principal_keys(
                conn, caller=principal, principal_id=principal_id
            )
        except ValueError as exc:
            raise ApiRejection(404, "not_found", str(exc)) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="keys.revoke_all",
            target=str(principal_id),
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True, "revoked": count}

    @app.get("/admin/quota/policies", response_model=QuotaPolicyResponse)
    def admin_quota_policies(
        request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        """List quota policies: one tenant for admins, every tenant for platform."""
        _need(principal, "admin:config")
        from libref.services.infra import quota

        scopes = set(getattr(principal, "scopes", set()) or ())
        scope_tenant = None if PLATFORM_SCOPE in scopes else _tenant_of(principal)
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            return envelope(quota.list_policies(rc, tenant_id=scope_tenant))
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.put("/admin/quota/policies", response_model=OkResponse)
    def admin_quota_policies_upsert(
        body: QuotaPolicyUpsert, request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        """Upsert one quota policy and drop the cached effective decision."""
        _need(principal, "admin:config")
        from libref.services.infra import quota
        from libref.services.infra.auth import principal_tenant
        from libref.services.ops.audit import write_audit

        target = (body.tenant_id or "").strip() or _write_tenant_of(principal)
        if not principal.can_administer(target):
            raise ApiRejection(403, "forbidden", "forbidden: target tenant")
        rc, should_close, est, bundle = _req_bundle(request, principal)
        try:
            if body.principal_id is not None:
                owner = principal_tenant(rc, body.principal_id)
                if owner is None:
                    raise ApiRejection(
                        400,
                        "invalid_argument",
                        f"unknown principal: {body.principal_id}",
                    )
                if owner != target:
                    raise ApiRejection(
                        400,
                        "invalid_argument",
                        "principal does not belong to the target tenant",
                    )
            try:
                quota.set_policy(
                    rc,
                    tenant_id=target,
                    resource=body.resource,
                    limit_value=body.limit_value,
                    window_type=body.window_type,
                    principal_id=body.principal_id,
                )
            except ValueError as exc:
                raise ApiRejection(400, "invalid_argument", str(exc)) from exc
            quota.invalidate_policy_cache()
            write_audit(
                rc,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="quota.policy",
                target=f"{target}:{body.principal_id or '*'}:{body.resource}",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            return {"ok": True}
        finally:
            if should_close:
                try:
                    rc.close()
                except Exception:  # noqa: BLE001, S110
                    pass

    @app.post("/admin/config/refresh", response_model=ConfigRefreshResponse)
    def admin_config_refresh(
        request: Request, principal=Depends(_guard)
    ) -> dict[str, Any]:
        _need(principal, "admin:config")
        from libref.config import load_settings as _load_settings
        from libref.observe import setup_logging as _setup_logging
        from libref.services.infra.context import refresh_context as _refresh_ctx
        from libref.services.ops.audit import write_audit

        old_fp = settings_fingerprint(settings)
        try:
            fresh = _load_settings()
        except Exception as exc:
            write_audit(
                conn,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="config.refresh",
                target=old_fp[:12],
                result="denied",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            raise ApiRejection(
                400, "config_refresh_failed", f"config refresh failed: {exc}"
            ) from exc
        new_fp = settings_fingerprint(fresh)
        try:
            ctx = getattr(request.app.state, "context", None)
            if ctx is not None:
                updated = _refresh_ctx(ctx, fresh)
                request.app.state.context = updated
                request.app.state.settings = fresh
            _setup_logging(fresh)
        except Exception as exc:
            write_audit(
                conn,
                tenant_id=_tenant_of(principal),
                principal_id=_principal_id(principal),
                action="config.refresh",
                target=f"{old_fp[:12]}->{new_fp[:12]}",
                result="failed",
                request_id=str(getattr(request.state, "request_id", "")),
            )
            raise ApiRejection(
                500, "config_refresh_failed", f"config rebuild failed: {exc}"
            ) from exc
        write_audit(
            conn,
            tenant_id=_tenant_of(principal),
            principal_id=_principal_id(principal),
            action="config.refresh",
            target=f"{old_fp[:12]}->{new_fp[:12]}",
            request_id=str(getattr(request.state, "request_id", "")),
        )
        return {"ok": True, "old_fingerprint": old_fp, "fingerprint": new_fp}

    def openapi_with_errors() -> dict[str, Any]:
        document = base_openapi()
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        error_schema = ErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        schemas.update(error_schema.pop("$defs", {}))
        schemas["ErrorResponse"] = error_schema
        validation_schema = ApiValidationError.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
        schemas.update(validation_schema.pop("$defs", {}))
        schemas["HTTPValidationError"] = validation_schema
        schemas.pop("ValidationError", None)
        error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
        for operations in document["paths"].values():
            for method, operation in operations.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                operation.setdefault("responses", {})["default"] = {
                    "description": "Rejection",
                    "content": {"application/json": {"schema": error_ref}},
                }
        return document

    app.openapi = openapi_with_errors  # type: ignore[method-assign]
