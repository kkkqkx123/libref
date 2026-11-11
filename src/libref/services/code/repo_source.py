"""Repository source lookup and dependency reads."""

from __future__ import annotations


def lookup_index_source_cached(
    conn, ecosystem: str, library: str, version: str
) -> tuple[str, str] | None:
    from pathlib import Path

    from libref.crawler.registry import canonical_ecosystem

    eco = canonical_ecosystem(ecosystem) or ecosystem
    repo_name = f"{eco}/{library}/{version}"
    row = conn.execute(
        "SELECT url, local_path FROM repos WHERE name=?", (repo_name,)
    ).fetchone()
    if row is not None:
        url, local_path = row[0], row[1]
        if local_path and Path(str(local_path)).is_dir():
            return str(local_path), "repos"
        if url:
            return str(url), "repos"
    lib_row = conn.execute(
        "SELECT repo_url FROM libraries WHERE ecosystem=? AND name=?",
        (eco, library),
    ).fetchone()
    if lib_row is not None and lib_row[0]:
        return str(lib_row[0]), "libraries"
    from libref.crawler.registry import SUPPORTED_ECOSYSTEMS

    if eco in SUPPORTED_ECOSYSTEMS:
        try:
            from libref.crawler.registry import normalize_name
            from libref.services.registry.resolver import read_cache

            norm = normalize_name(eco, library)
            cached = read_cache(conn, eco, norm)
        except ValueError:
            cached = None
        if cached is not None and cached.get("repo_url"):
            return str(cached["repo_url"]), "registry-cache"
    return None


def list_repo_dependencies(
    conn,
    library: str,
    version: str,
    dep_ecosystem: str | None = None,
    *,
    ecosystem: str | None = None,
) -> list[dict]:
    """Read model over manifest-declared dependency edges for one library version.

    The positional filter selects the dependency ecosystem; the keyword-only
    filter selects the owning library ecosystem for same-name disambiguation."""
    from libref.crawler.registry import canonical_ecosystem

    owner = canonical_ecosystem(ecosystem) if ecosystem is not None else None
    if ecosystem is not None and owner is None:
        raise ValueError(f"unsupported ecosystem: {ecosystem}")
    dep = canonical_ecosystem(dep_ecosystem) if dep_ecosystem is not None else None
    if dep_ecosystem is not None and dep is None:
        raise ValueError(f"unsupported ecosystem: {dep_ecosystem}")
    query = (
        "SELECT d.ecosystem, d.name, d.req, d.source_file"
        " FROM repo_dependencies d JOIN versions v ON v.id = d.version_id"
        " JOIN libraries l ON l.id = d.library_id"
        " WHERE l.name = ? AND v.version = ?"
    )
    params: list[str] = [library, version]
    if owner is not None:
        query += " AND l.ecosystem = ?"
        params.append(owner)
    if dep is not None:
        query += " AND d.ecosystem = ?"
        params.append(dep)
    rows = conn.execute(query + " ORDER BY d.ecosystem, d.name", params).fetchall()
    return [
        {
            "ecosystem": r[0],
            "name": r[1],
            "req": r[2],
            "source_file": r[3],
        }
        for r in rows
    ]
