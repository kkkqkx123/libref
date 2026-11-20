"""Usage corpus sources and reproducible mining profiles."""

from __future__ import annotations

import hashlib
import json
from typing import Any

SOURCE_ROLES = frozenset(
    {
        "official_example",
        "doc_snippet",
        "readme",
        "test",
        "reference_project",
        "implementation",
    }
)
USAGE_ROLES = SOURCE_ROLES - {"implementation"}
ORIGIN_KINDS = frozenset(
    {
        "repo_file",
        "html_code_block",
        "markdown_fence",
        "rustdoc_doctest",
        "manual_snippet",
    }
)
DEFAULT_PROFILE = "usage"


def _dump(value: Any) -> str:
    """Stable JSON text for identity and audit fields."""
    if value is None:
        return "{}"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except ValueError:
        return default


def register_source(
    conn,
    *,
    version_id: int,
    source_key: str,
    source_role: str,
    origin_kind: str = "repo_file",
    source_url: str = "",
    commit_sha: str = "",
    selection: Any = None,
    target_filter: Any = None,
    weight: float = 1.0,
    limits: Any = None,
    enabled: bool = True,
    tenant_id: str = "default",
) -> int:
    """Register or refresh one usage corpus source for a target version."""
    if source_role not in SOURCE_ROLES:
        raise ValueError(f"unsupported source role: {source_role}")
    if origin_kind not in ORIGIN_KINDS:
        raise ValueError(f"unsupported origin kind: {origin_kind}")
    if not str(source_key).strip():
        raise ValueError("source_key must not be empty")
    row = conn.execute(
        "SELECT id FROM code_sources WHERE version_id=? AND source_key=?",
        (int(version_id), str(source_key)),
    ).fetchone()
    values = (
        int(version_id),
        str(source_key),
        str(source_role),
        str(origin_kind),
        str(source_url or ""),
        str(commit_sha or ""),
        _dump(selection),
        _dump(target_filter),
        float(weight),
        _dump(limits),
        1 if enabled else 0,
        str(tenant_id),
    )
    if row is None:
        cur = conn.execute(
            "INSERT INTO code_sources(version_id, source_key, source_role, origin_kind,"
            " source_url, commit_sha, selection, target_filter, weight, limits, enabled, tenant_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE code_sources SET source_role=?, origin_kind=?, source_url=?, commit_sha=?,"
        " selection=?, target_filter=?, weight=?, limits=?, enabled=?, tenant_id=?"
        " WHERE id=?",
        values[2:] + (int(row[0]),),
    )
    return int(row[0])


def list_sources(
    conn,
    version_id: int,
    *,
    enabled_only: bool = False,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Read registered sources for one version."""
    sql = "SELECT * FROM code_sources WHERE version_id=?"
    params: list[Any] = [int(version_id)]
    if enabled_only:
        sql += " AND enabled=1"
    if tenant_id not in (None, "*"):
        sql += " AND tenant_id=?"
        params.append(str(tenant_id))
    sql += " ORDER BY id"
    return [_source_dict(row) for row in conn.execute(sql, params).fetchall()]


def _source_dict(row) -> dict[str, Any]:
    keys = (
        "id",
        "version_id",
        "source_key",
        "source_role",
        "origin_kind",
        "source_url",
        "commit_sha",
        "selection",
        "target_filter",
        "weight",
        "limits",
        "enabled",
        "tenant_id",
    )
    item = {key: row[idx] for idx, key in enumerate(keys)}
    item["selection"] = _load(item["selection"], {})
    item["target_filter"] = _load(item["target_filter"], {})
    item["limits"] = _load(item["limits"], {})
    item["enabled"] = bool(item["enabled"])
    return item


def _fingerprint(source_ids: list[int], support_mode: str, params: Any) -> str:
    blob = _dump(
        {
            "sources": sorted(int(v) for v in source_ids),
            "support": support_mode,
            "params": params or {},
        }
    ).encode()
    return hashlib.sha1(blob).hexdigest()


def ensure_profile(
    conn,
    *,
    version_id: int,
    name: str,
    source_ids: list[int],
    support_mode: str = "sample",
    params: Any = None,
    tenant_id: str = "default",
) -> int:
    """Create or refresh one mining profile and return its identity."""
    if not str(name).strip():
        raise ValueError("profile name must not be empty")
    ids = sorted({int(v) for v in source_ids})
    fingerprint = _fingerprint(ids, support_mode, params)
    row = conn.execute(
        "SELECT id, fingerprint FROM mining_profiles WHERE version_id=? AND name=?",
        (int(version_id), str(name)),
    ).fetchone()
    values = (
        _dump(ids),
        str(support_mode),
        _dump(params),
        fingerprint,
        str(tenant_id),
    )
    if row is None:
        cur = conn.execute(
            "INSERT INTO mining_profiles(version_id, name, source_ids, support_mode,"
            " params, fingerprint, tenant_id) VALUES(?,?,?,?,?,?,?)",
            (int(version_id), str(name), *values),
        )
        return int(cur.lastrowid)
    conn.execute(
        "UPDATE mining_profiles SET source_ids=?, support_mode=?, params=?,"
        " fingerprint=?, tenant_id=? WHERE id=?",
        (*values, int(row[0])),
    )
    return int(row[0])


def _usage_source_ids(conn, version_id: int, tenant_id: str) -> list[int]:
    sql = (
        "SELECT id FROM code_sources WHERE version_id=? AND enabled=1"
        " AND source_role!='implementation'"
    )
    params: list[Any] = [int(version_id)]
    if tenant_id not in (None, "*"):
        sql += " AND tenant_id=?"
        params.append(str(tenant_id))
    sql += " ORDER BY id"
    return [int(r[0]) for r in conn.execute(sql, params).fetchall()]


def resolve_profile(
    conn,
    *,
    version_id: int,
    name: str | None = None,
    source_ids: list[int] | None = None,
    source_roles: list[str] | tuple[str, ...] | None = None,
    support_mode: str = "sample",
    tenant_id: str = "default",
) -> dict[str, Any]:
    """Resolve a profile for a mining run, creating the default usage profile."""
    if source_ids is not None:
        ids = sorted({int(v) for v in source_ids})
    elif name in ("implementation", "all"):
        sql = (
            "SELECT id, source_role FROM code_sources WHERE version_id=? AND enabled=1"
        )
        params: list[Any] = [int(version_id)]
        if tenant_id not in (None, "*"):
            sql += " AND tenant_id=?"
            params.append(str(tenant_id))
        rows = conn.execute(sql, params).fetchall()
        if name == "implementation":
            ids = sorted(int(r[0]) for r in rows if str(r[1]) == "implementation")
        else:
            ids = sorted(int(r[0]) for r in rows)
        profile_name = str(name)
        pid = ensure_profile(
            conn,
            version_id=int(version_id),
            name=profile_name,
            source_ids=ids,
            support_mode=support_mode,
            params={},
            tenant_id=tenant_id,
        )
        return {
            "id": pid,
            "name": profile_name,
            "source_ids": ids,
            "support_mode": support_mode,
        }
    elif name is not None and str(name) != DEFAULT_PROFILE:
        row = conn.execute(
            "SELECT id, source_ids, support_mode FROM mining_profiles"
            " WHERE version_id=? AND name=?",
            (int(version_id), str(name)),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown mining profile: {name}")
        return {
            "id": int(row[0]),
            "name": str(name),
            "source_ids": [int(v) for v in _load(row[1], [])],
            "support_mode": str(row[2]),
        }
    else:
        ids = _usage_source_ids(conn, version_id, tenant_id)
        if source_roles is not None:
            wanted = sorted({str(r) for r in source_roles if str(r).strip()})
            marks = ",".join("?" for _ in wanted)
            rows = conn.execute(
                "SELECT id FROM code_sources WHERE version_id=? AND enabled=1"
                f" AND source_role IN ({marks}) ORDER BY id",
                (int(version_id), *wanted),
            ).fetchall()
            ids = sorted({int(r[0]) for r in rows})
    profile_name = name or DEFAULT_PROFILE
    pid = ensure_profile(
        conn,
        version_id=int(version_id),
        name=profile_name,
        source_ids=ids,
        support_mode=support_mode,
        params={"source_roles": sorted(set(source_roles or []))},
        tenant_id=tenant_id,
    )
    return {
        "id": pid,
        "name": profile_name,
        "source_ids": ids,
        "support_mode": support_mode,
    }


def profile_id_for_version(
    conn,
    version_id: int,
    *,
    name: str | None = None,
    tenant_id: str = "default",
) -> int | None:
    """Resolve an existing profile id for read paths without creating state."""
    profile_name = name or DEFAULT_PROFILE
    row = conn.execute(
        "SELECT id FROM mining_profiles WHERE version_id=? AND name=?",
        (int(version_id), profile_name),
    ).fetchone()
    if row is not None:
        return int(row[0])
    if name is not None:
        return None
    return None


def source_map(conn, version_id: int) -> dict[int, dict[str, Any]]:
    """Source lookup by id for evidence rendering."""
    return {int(item["id"]): item for item in list_sources(conn, int(version_id))}


def matches_target(callee: str, target_filter: Any) -> bool:
    """Whether one resolved call belongs to the configured target API set."""
    filt = _load(target_filter, {})
    if not isinstance(filt, dict) or not filt:
        return True
    names = {str(v) for v in filt.get("names", []) if str(v).strip()}
    prefixes = tuple(str(v) for v in filt.get("prefixes", []) if str(v).strip())
    value = str(callee or "")
    if names and value in names:
        return True
    if prefixes and value.startswith(prefixes):
        return True
    return not names and not prefixes
