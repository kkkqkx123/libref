"""Feature tree, facet and cluster derived table refresh."""

from __future__ import annotations

from libref.services.code.mining_refresh.induction import (
    _label_table_ready,
    _table_ready,
)


def _cluster_defaults(
    threshold: float | None, min_size: int | None
) -> tuple[float, int]:
    """When cluster params are missing, resolve through the full settings precedence chain (env and config file included)."""
    if threshold is not None and min_size is not None:
        return threshold, min_size
    from libref.config import LibrefSettings

    settings = LibrefSettings()
    resolved_threshold = settings.cluster_threshold if threshold is None else threshold
    resolved_size = settings.cluster_min_size if min_size is None else min_size
    return float(resolved_threshold), int(resolved_size)


def _facet_defaults(
    topn: int | None,
    candidate_budget: int | None,
    min_gain: float | None,
    guard_enabled: bool | None,
) -> tuple[int, int, float, bool]:
    """Fill missing facet selection params from the settings precedence chain."""
    if (
        topn is not None
        and candidate_budget is not None
        and min_gain is not None
        and guard_enabled is not None
    ):
        return topn, candidate_budget, min_gain, guard_enabled
    from libref.config import LibrefSettings

    settings = LibrefSettings()
    return (
        int(settings.facet_topn) if topn is None else topn,
        int(settings.facet_candidate_budget)
        if candidate_budget is None
        else candidate_budget,
        float(settings.facet_min_gain) if min_gain is None else min_gain,
        bool(settings.guard_enabled) if guard_enabled is None else guard_enabled,
    )


def _clear_derived(conn, version_id: int, profile_id: int) -> None:
    """Clear derived outputs for one mining profile."""
    conn.execute(
        "DELETE FROM seq_patterns WHERE version_id=? AND profile_id=?",
        (version_id, profile_id),
    )
    conn.execute(
        "DELETE FROM feature_nodes WHERE version_id=? AND profile_id=?",
        (version_id, profile_id),
    )
    conn.execute(
        "DELETE FROM api_clusters WHERE version_id=? AND profile_id=?",
        (version_id, profile_id),
    )
    if _label_table_ready(conn):
        conn.execute(
            "DELETE FROM pattern_labels WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
    if _table_ready(conn, "feature_facets"):
        conn.execute(
            "DELETE FROM feature_facets WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
    if _table_ready(conn, "cluster_labels"):
        conn.execute(
            "DELETE FROM cluster_labels WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )


def _delete_not_in(
    conn, table: str, version_id: int, profile_id: int, column: str, wanted: list
) -> int:
    """Set-difference cleanup within one profile."""
    if not wanted:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
        return int(cur.rowcount or 0)
    keep = set(wanted)
    existing = [
        r[0]
        for r in conn.execute(
            f"SELECT {column} FROM {table} WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        ).fetchall()
    ]
    dead = [v for v in existing if v not in keep]
    removed = 0
    for start in range(0, len(dead), 500):
        batch = dead[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        cur = conn.execute(
            f"DELETE FROM {table} WHERE version_id=? AND profile_id=?"
            f" AND {column} IN ({placeholders})",
            (version_id, profile_id, *batch),
        )
        removed += int(cur.rowcount or 0)
    return removed


def refresh_feature_tree(
    conn,
    library_id: int,
    version_id: int,
    *,
    rows: list,
    profile_id: int,
) -> int:
    """Rebuild hierarchical feature nodes from one shared call set."""
    from libref.mining import feature_tree as _tree
    from libref.mining import scope_key as _scope_key

    calls = [
        (str(r[0]), str(r[10]) or _scope_key(str(r[1]), r[2])) for r in rows if r[0]
    ]
    if not calls:
        conn.execute(
            "DELETE FROM feature_nodes WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
        return 0
    entity_rows = conn.execute(
        "SELECT qualified_name, kind, parent FROM api_entities WHERE version_id=?",
        (version_id,),
    ).fetchall()
    kinds = {str(r[0]): str(r[1]) for r in entity_rows}
    parents = {str(r[0]): (str(r[2]) if r[2] else None) for r in entity_rows}
    nodes = _tree.build_feature_tree(calls, kinds, parents)
    wanted = [n["path"] for n in nodes]
    conn.executemany(
        "INSERT INTO feature_nodes(library_id, version_id, profile_id, path, parent,"
        " depth, kind, calls, scopes) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(profile_id, path) DO UPDATE SET parent=excluded.parent,"
        " depth=excluded.depth, kind=excluded.kind,"
        " calls=excluded.calls, scopes=excluded.scopes",
        [
            (
                library_id,
                version_id,
                profile_id,
                node["path"],
                node["parent"],
                int(node["depth"]),
                node["kind"],
                int(node["calls"]),
                int(node["scopes"]),
            )
            for node in nodes
        ],
    )
    _delete_not_in(conn, "feature_nodes", version_id, profile_id, "path", wanted)
    return len(nodes)


def refresh_facets(
    conn,
    library_id: int,
    version_id: int,
    *,
    rows: list,
    profile_id: int,
    facet_topn: int | None = None,
    candidate_budget: int | None = None,
    min_gain: float | None = None,
    guard_enabled: bool | None = None,
) -> int:
    """Rebuild usage facet rows in bulk; returns the facet row count."""
    import json as _json

    from libref.mining import facets as _facets
    from libref.mining import scope_key as _scope_key

    topn, budget, gain, guards = _facet_defaults(
        facet_topn, candidate_budget, min_gain, guard_enabled
    )
    if not _table_ready(conn, "feature_facets"):
        return 0
    if not rows:
        conn.execute(
            "DELETE FROM feature_facets WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
        return 0
    facet_rows: list[dict] = []
    for r in rows:
        if not r[0]:
            continue
        try:
            parsed = _json.loads(r[13]) if len(r) > 13 and r[13] else []
        except ValueError:
            parsed = [r[13]] if len(r) > 13 and r[13] else []
        facet_rows.append(
            {
                "callee": str(r[0]),
                "args": list(parsed) if isinstance(parsed, list) else [parsed],
                "relation": str(r[11] or "direct"),
                "scope": str(r[10]) or _scope_key(str(r[1]), r[2]),
                "line": int(r[5]),
                "order": int(r[6]),
            }
        )
    selected = _facets.aggregate_facets(
        facet_rows,
        guard_enabled=guards,
        candidate_budget=budget,
        topn=topn,
        min_gain=gain,
    )
    wanted = [f"{row['facet']}\0{row['value']}" for row in selected]
    if not wanted:
        conn.execute(
            "DELETE FROM feature_facets WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
        return 0
    conn.executemany(
        "INSERT INTO feature_facets(library_id, version_id, profile_id, facet,"
        " facet_key, value, calls, scopes) VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(profile_id, facet_key) DO UPDATE SET value=excluded.value,"
        " calls=excluded.calls, scopes=excluded.scopes",
        [
            (
                library_id,
                version_id,
                profile_id,
                str(row["facet"]),
                f"{row['facet']}\0{row['value']}",
                str(row["value"]),
                int(row["calls"]),
                int(row["scopes"]),
            )
            for row in selected
        ],
    )
    _delete_not_in(conn, "feature_facets", version_id, profile_id, "facet_key", wanted)
    return len(selected)


def refresh_clusters(
    conn,
    library_id: int,
    version_id: int,
    *,
    rows: list,
    threshold: float | None = None,
    min_size: int | None = None,
    profile_id: int,
) -> int:
    """Rebuild API clusters from one shared call set."""
    from libref.mining import build_scope_sets as _scope_sets
    from libref.mining import scope_key as _scope_key
    from libref.mining.cluster import cluster_apis as _cluster_fn

    threshold, min_size = _cluster_defaults(threshold, min_size)

    triplets = [
        (str(r[0]), str(r[10]) or _scope_key(str(r[1]), r[2]), r[2])
        for r in rows
        if r[0]
    ]
    sets = _scope_sets(triplets)
    groups = _cluster_fn(sets, threshold=threshold, min_size=min_size)
    wanted = sorted({m for g in groups for m in g["members"]})
    if not wanted:
        conn.execute(
            "DELETE FROM api_clusters WHERE version_id=? AND profile_id=?",
            (version_id, profile_id),
        )
        if _table_ready(conn, "cluster_labels"):
            conn.execute(
                "DELETE FROM cluster_labels WHERE version_id=? AND profile_id=?",
                (version_id, profile_id),
            )
        return 0
    conn.executemany(
        "INSERT INTO api_clusters(library_id, version_id, profile_id, cluster_id,"
        " member, size, representative) VALUES(?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(profile_id, member) DO UPDATE SET cluster_id=excluded.cluster_id,"
        " size=excluded.size, representative=excluded.representative",
        [
            (
                library_id,
                version_id,
                profile_id,
                int(group["cluster_id"]),
                member,
                int(group["size"]),
                group["representative"],
            )
            for group in groups
            for member in group["members"]
        ],
    )
    _delete_not_in(conn, "api_clusters", version_id, profile_id, "member", wanted)
    if _table_ready(conn, "cluster_labels"):
        from libref.mining.induction import cluster_key as _cluster_key

        live_keys = [_cluster_key(list(g["members"])) for g in groups]
        _delete_not_in(
            conn, "cluster_labels", version_id, profile_id, "cluster_key", live_keys
        )
    return len(groups)


def _refresh_doc_derived(
    conn,
    library: str,
    version: str,
    profile: str | None,
    tenant_id: str,
    ecosystem: str | None = None,
) -> None:
    """Rebuild document coverage after a pattern refresh, without failing mining."""
    try:
        from libref.services.docs.feature_refresh import refresh_doc_features

        identity = {"ecosystem": ecosystem} if ecosystem is not None else {}
        refresh_doc_features(
            conn,
            library,
            version,
            profile=profile,
            scope="derived",
            tenant_id=tenant_id,
            **identity,
        )
    except Exception as exc:  # noqa: BLE001 — mining truth is already committed
        import logging

        logging.getLogger(__name__).warning("document coverage refresh failed: %s", exc)
    from libref.services.registry.libraries import LibraryService, mining_version_ids

    catalog = LibraryService(conn)
    version_ids = mining_version_ids(
        conn, library, version, ecosystem or "custom", tenant_id=tenant_id
    )
    if not version_ids:
        return
    source_version_id = int(version_ids[0])
    for bound in catalog.bound_doc_versions(source_version_id, tenant_id=tenant_id):
        if int(bound["version_id"]) == source_version_id:
            continue
        _refresh_doc_derived(
            conn,
            str(bound["library"]),
            str(bound["version"]),
            None,
            tenant_id,
            str(bound["ecosystem"]),
        )
