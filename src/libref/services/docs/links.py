"""Document link edges: per-version source page linkage plus reference-count reads."""

from __future__ import annotations

from typing import Any


def replace_version_edges(
    conn, library_id: int, version_id: int, edges: dict[str, set[tuple[str, str]]]
) -> int:
    """Replace edges for the visited source pages; resumed-skipped pages keep old rows."""
    from libref.db.conn import write_lock

    srcs = [src for src, dsts in edges.items() if dsts]
    if not srcs:
        return 0
    rows = [
        (library_id, version_id, src, dst, kind)
        for src, dsts in edges.items()
        for dst, kind in sorted(dsts)
    ]
    marks = ",".join("?" for _ in srcs)
    with write_lock(conn):
        conn.execute(
            f"DELETE FROM doc_link_edges WHERE version_id=? AND src_url IN ({marks})",
            (version_id, *srcs),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO doc_link_edges"
            "(library_id, version_id, src_url, dst_url, kind) VALUES(?,?,?,?,?)",
            rows,
        )
    return len(rows)


def prune_version_urls(conn, version_id: int, src_urls: list[str]) -> int:
    """Drop edges whose source page was swept; destinations are kept."""
    if not src_urls:
        return 0
    from libref.db.conn import write_lock

    marks = ",".join("?" for _ in src_urls)
    with write_lock(conn):
        cur = conn.execute(
            f"DELETE FROM doc_link_edges WHERE version_id=? AND src_url IN ({marks})",
            (version_id, *src_urls),
        )
    return cur.rowcount if cur.rowcount is not None else 0


def _resolve_version(
    conn, library: str, version: str, ecosystem: str | None
) -> tuple[int, int]:
    query = (
        "SELECT l.id, v.id FROM versions v JOIN libraries l ON l.id = v.library_id"
        " WHERE l.name=? AND v.version=?"
    )
    params: list[Any] = [library, version]
    if ecosystem is not None:
        query += " AND l.ecosystem=?"
        params.append(ecosystem)
    rows = conn.execute(query, params).fetchall()
    if not rows:
        raise ValueError(f"unknown library version: {library} {version}")
    if len(rows) > 1:
        raise ValueError(f"ambiguous library version across ecosystems: {library} {version}")
    return int(rows[0][0]), int(rows[0][1])


def version_link_summary(
    conn, library: str, version: str, ecosystem: str | None = None
) -> dict[str, Any]:
    """Edge totals plus per-kind distribution for one library version."""
    _library_id, version_id = _resolve_version(conn, library, version, ecosystem)
    rows = conn.execute(
        "SELECT kind, COUNT(*) FROM doc_link_edges WHERE version_id=? GROUP BY kind",
        (version_id,),
    ).fetchall()
    kinds = {str(kind): int(n) for kind, n in rows}
    pages = conn.execute(
        "SELECT COUNT(DISTINCT src_url) FROM doc_link_edges WHERE version_id=?",
        (version_id,),
    ).fetchone()[0]
    return {
        "library": library,
        "version": version,
        "pages_with_edges": int(pages),
        "edges": sum(kinds.values()),
        "internal_edges": kinds.get("internal", 0),
        "external_edges": kinds.get("external", 0),
        "structure_edges": kinds.get("structure", 0),
    }


def top_referenced(
    conn,
    library: str,
    version: str,
    ecosystem: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Most referenced target URLs within one library version, by inlink count."""
    _library_id, version_id = _resolve_version(conn, library, version, ecosystem)
    bound = f" LIMIT {max(1, int(limit))}" if limit is not None else ""
    rows = conn.execute(
        "SELECT dst_url, COUNT(DISTINCT src_url) AS inlinks FROM doc_link_edges"
        f" WHERE version_id=? GROUP BY dst_url ORDER BY inlinks DESC, dst_url ASC{bound}",
        (version_id,),
    ).fetchall()
    return [{"url": str(url), "inlinks": int(n)} for url, n in rows]


def page_link_stats(
    conn, url: str, library: str, version: str, ecosystem: str | None = None
) -> dict[str, Any]:
    """Outlink and inlink counts for one page, plus sampled inlink sources."""
    _library_id, version_id = _resolve_version(conn, library, version, ecosystem)
    out = conn.execute(
        "SELECT COUNT(*), SUM(kind='external') FROM doc_link_edges"
        " WHERE version_id=? AND src_url=?",
        (version_id, url),
    ).fetchone()
    inlinks = conn.execute(
        "SELECT COUNT(DISTINCT src_url) FROM doc_link_edges"
        " WHERE version_id=? AND dst_url=?",
        (version_id, url),
    ).fetchone()[0]
    sources = conn.execute(
        "SELECT DISTINCT src_url FROM doc_link_edges"
        " WHERE version_id=? AND dst_url=? ORDER BY src_url ASC LIMIT 20",
        (version_id, url),
    ).fetchall()
    return {
        "url": url,
        "outlinks": int(out[0]),
        "outlink_external": int(out[1] or 0),
        "inlinks": int(inlinks),
        "inlink_sources": [str(r[0]) for r in sources],
    }
