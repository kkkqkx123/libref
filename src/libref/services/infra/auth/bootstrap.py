"""Initial administrator bootstrap."""

from __future__ import annotations

from libref.db.contract import MetadataSession
from libref.services.infra.auth.identity import PLATFORM_ROLE, Principal, _system_caller
from libref.services.infra.auth.keys import _insert_key, lookup_principal
from libref.services.infra.auth.principals import create_principal
from libref.services.infra.auth.storage import ensure_auth_tables
from libref.services.infra.tenant import GLOBAL_TENANT


def bootstrap_admin(conn: MetadataSession, raw_key: str, settings) -> Principal:
    """Idempotently create the first platform administrator from a bootstrap key."""
    ensure_auth_tables(conn)
    existing = lookup_principal(conn, raw_key)
    if existing is not None:
        return existing
    row = conn.execute("SELECT id FROM principals LIMIT 1").fetchone()
    if row is not None:
        raise ValueError("bootstrap key does not match any issued key")
    system = _system_caller()
    principal_id = create_principal(
        conn,
        caller=system,
        tenant_id=GLOBAL_TENANT,
        kind="human",
        name="bootstrap-admin",
        roles=[PLATFORM_ROLE],
    )
    _insert_key(
        conn,
        caller=system,
        settings=settings,
        principal_id=principal_id,
        raw=raw_key,
        scopes=[],
        expires_at=None,
    )
    found = lookup_principal(conn, raw_key)
    assert found is not None
    return found
