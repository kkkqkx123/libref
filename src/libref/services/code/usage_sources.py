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


def update_source_commit(conn, source_id: int, commit_sha: str) -> None:
    """Fill in the commit after a clone establishes the pinned source identity."""
    conn.execute(
        "UPDATE code_sources SET commit_sha=? WHERE id=?",
        (str(commit_sha or ""), int(source_id)),
    )


def _source_identities(conn, source_ids: list[int]) -> list[str]:
    """Stable identity text for every source participating in a profile."""
    ids = sorted({int(v) for v in source_ids})
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT id, source_key, source_role, origin_kind, source_url, commit_sha,"
        " selection, target_filter, weight, limits, enabled, tenant_id"
        f" FROM code_sources WHERE id IN ({marks})",
        ids,
    ).fetchall()
    identities: list[str] = []
    for row in rows:
        identities.append(
            _dump(
                (
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                    _load(row[6], {}),
                    _load(row[7], {}),
                    float(row[8]),
                    _load(row[9], {}),
                    bool(row[10]),
                    str(row[11]),
                )
            )
        )
    return sorted(identities)


def _fingerprint(conn, source_ids: list[int], support_mode: str, params: Any) -> str:
    blob = _dump(
        {
            "sources": _source_identities(conn, source_ids),
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
    fingerprint = _fingerprint(conn, ids, support_mode, params)
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
    """Resolve a mining profile without rewriting the default usage profile."""
    overrides = source_ids is not None or source_roles is not None
    wanted_roles: list[str] = []
    if source_roles is not None:
        wanted_roles = sorted({str(r).strip() for r in source_roles if str(r).strip()})
        unknown = [role for role in wanted_roles if role not in SOURCE_ROLES]
        if unknown:
            raise ValueError(f"unsupported source role: {unknown[0]}")
    params: dict[str, Any] = {"source_roles": wanted_roles} if wanted_roles else {}

    if source_ids is not None:
        ids = sorted({int(v) for v in source_ids})
    elif name in ("implementation", "all"):
        sql = (
            "SELECT id, source_role FROM code_sources WHERE version_id=? AND enabled=1"
        )
        query_params: list[Any] = [int(version_id)]
        if tenant_id not in (None, "*"):
            sql += " AND tenant_id=?"
            query_params.append(str(tenant_id))
        rows = conn.execute(sql, query_params).fetchall()
        if name == "implementation":
            ids = sorted(int(r[0]) for r in rows if str(r[1]) == "implementation")
        else:
            ids = sorted(int(r[0]) for r in rows)
        pid = ensure_profile(
            conn,
            version_id=int(version_id),
            name=str(name),
            source_ids=ids,
            support_mode=support_mode,
            params={},
            tenant_id=tenant_id,
        )
        return {
            "id": pid,
            "name": str(name),
            "source_ids": ids,
            "support_mode": support_mode,
        }
    else:
        ids = _usage_source_ids(conn, version_id, tenant_id)
        if wanted_roles:
            marks = ",".join("?" for _ in wanted_roles)
            sql = (
                "SELECT id FROM code_sources WHERE version_id=? AND enabled=1"
                f" AND source_role IN ({marks})"
            )
            query_params = [int(version_id), *wanted_roles]
            if tenant_id not in (None, "*"):
                sql += " AND tenant_id=?"
                query_params.append(str(tenant_id))
            sql += " ORDER BY id"
            rows = conn.execute(sql, query_params).fetchall()
            ids = sorted({int(r[0]) for r in rows})

    profile_name = name or DEFAULT_PROFILE
    if name is not None and name not in (DEFAULT_PROFILE, "implementation", "all"):
        row = conn.execute(
            "SELECT id, source_ids, support_mode FROM mining_profiles"
            " WHERE version_id=? AND name=?",
            (int(version_id), str(name)),
        ).fetchone()
        if row is not None and not overrides:
            return {
                "id": int(row[0]),
                "name": str(name),
                "source_ids": [int(v) for v in _load(row[1], [])],
                "support_mode": str(row[2]),
            }
        if row is not None and overrides:
            suffix = _fingerprint(conn, ids, support_mode, params)[:12]
            profile_name = f"{name}:{suffix}"
    elif overrides:
        suffix = _fingerprint(conn, ids, support_mode, params)[:12]
        profile_name = f"{DEFAULT_PROFILE}:{suffix}"

    pid = ensure_profile(
        conn,
        version_id=int(version_id),
        name=profile_name,
        source_ids=ids,
        support_mode=support_mode,
        params=params,
        tenant_id=tenant_id,
    )
    return {
        "id": pid,
        "name": profile_name,
        "source_ids": ids,
        "support_mode": support_mode,
    }
