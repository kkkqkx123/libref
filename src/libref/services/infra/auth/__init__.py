"""Identity and access management facade for all exits and services."""

from __future__ import annotations

from libref.services.infra.auth.bootstrap import *
from libref.services.infra.auth.identity import *
from libref.services.infra.auth.identity import _parse_json_list, _system_caller
from libref.services.infra.auth.keys import *
from libref.services.infra.auth.keys import _insert_key
from libref.services.infra.auth.principals import *
from libref.services.infra.auth.principals import _validate_roles
from libref.services.infra.auth.request_auth import *
from libref.services.infra.auth.roles import *
from libref.services.infra.auth.storage import *
from libref.services.infra.auth.storage import _principal_from_row, _principal_row

__all__ = [
    "KNOWN_SCOPES",
    "PLATFORM_ROLE",
    "PLATFORM_SCOPE",
    "READ_SCOPES",
    "ROLE_SCOPES",
    "ROLE_SEPARATION_EXEMPT",
    "ROLE_SEPARATION_GROUPS",
    "ApiKeySummary",
    "AuthDenied",
    "Principal",
    "PrincipalSummary",
    "RoleConflict",
    "RoleSplitPlan",
    "SplitPart",
    "SplitResult",
    "_expiry",
    "_insert_key",
    "_origin_key",
    "_parse_json_list",
    "_principal_from_row",
    "_principal_row",
    "_system_caller",
    "_validate_roles",
    "authenticate_request",
    "bootstrap_admin",
    "check_role_separation",
    "check_startup",
    "create_principal",
    "current_principal",
    "dev_principal",
    "ensure_auth_tables",
    "hash_key",
    "is_allowed_origin",
    "is_loopback_host",
    "issue_api_key",
    "list_api_keys",
    "list_principals",
    "lookup_principal",
    "missing_scopes",
    "pack_roles",
    "parse_bearer",
    "require_any_scope",
    "require_scopes",
    "revoke_api_key",
    "revoke_principal_keys",
    "role_separation_violations",
    "scan_role_conflicts",
    "scopes_for_roles",
    "separation_advice",
    "set_principal_roles",
    "split_role_conflict",
    "system_caller",
    "validate_key_scopes",
    "validate_origin_config",
    "verify_hash",
    "verify_key",
    "write_tenant",
]
