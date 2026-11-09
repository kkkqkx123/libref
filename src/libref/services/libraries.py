"""Library and version records: the only place that writes libraries/versions."""

from __future__ import annotations

from typing import Any


def mining_version_ids(
    conn,
    library: str | None = None,
    version: str | None = None,
    ecosystem: str | None = None,
) -> list[int]:
    """Resolve library filters to version ids."""
    filters = []
    params: list[Any] = []
    if library is not None:
        filters.append("l.name = ?")
        params.append(library)
    if ecosystem is not None:
        filters.append("l.ecosystem = ?")
        params.append(ecosystem)
    if version is not None:
        filters.append("v.version = ?")
        params.append(version)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id "
        f"{where}",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


class LibraryService:
    """Library and version catalog reads and ensure-or-create writes."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def list_libraries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT l.id, l.ecosystem, l.name, l.description, l.docs_url, l.resolved_from, "
            "(SELECT count(*) FROM versions v WHERE v.library_id = l.id) AS versions "
            "FROM libraries l ORDER BY l.ecosystem, l.name"
        ).fetchall()
        return [
            {
                "id": r[0],
                "ecosystem": r[1],
                "name": r[2],
                "description": r[3],
                "docs_url": r[4],
                "resolved_from": r[5],
                "versions": r[6],
            }
            for r in rows
        ]

    def list_versions(
        self, library: str, ecosystem: str | None = None
    ) -> list[dict[str, Any]]:
        if ecosystem is None:
            rows = self._conn.execute(
                "SELECT v.id, v.version, v.registry_confirmed, v.created_at, l.ecosystem, "
                "(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages "
                "FROM versions v JOIN libraries l ON l.id = v.library_id "
                "WHERE l.name = ? ORDER BY v.created_at",
                (library,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT v.id, v.version, v.registry_confirmed, v.created_at, l.ecosystem, "
                "(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages "
                "FROM versions v JOIN libraries l ON l.id = v.library_id "
                "WHERE l.name = ? AND l.ecosystem = ? ORDER BY v.created_at",
                (library, ecosystem),
            ).fetchall()
        return [
            {
                "id": r[0],
                "version": r[1],
                "registry_confirmed": bool(r[2]),
                "created_at": r[3],
                "ecosystem": r[4],
                "pages": r[5],
            }
            for r in rows
        ]

    def ensure_library_version(
        self,
        library: str,
        version: str,
        ecosystem: str = "custom",
        *,
        homepage: str | None = None,
        docs_url: str | None = None,
        repo_url: str | None = None,
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
    ) -> tuple[int, int]:
        """Ensure library/version rows exist, return (library_id, version_id)."""
        self._conn.execute(
            "INSERT INTO libraries(ecosystem, name) VALUES(?,?) "
            "ON CONFLICT(ecosystem, name) DO NOTHING",
            (ecosystem, library),
        )
        row = self._conn.execute(
            "SELECT id FROM libraries WHERE ecosystem=? AND name=?", (ecosystem, library)
        ).fetchone()
        library_id = int(row[0])
        if homepage is not None or docs_url is not None or repo_url is not None or resolved_from is not None:
            self._conn.execute(
                "UPDATE libraries SET homepage=COALESCE(?, homepage), docs_url=COALESCE(?, docs_url), "
                "repo_url=COALESCE(?, repo_url), resolved_from=COALESCE(?, resolved_from), "
                "resolved_at=datetime('now') WHERE id=?",
                (homepage, docs_url, repo_url, resolved_from, library_id),
            )
        self._conn.execute(
            "INSERT INTO versions(library_id, version, registry_confirmed) VALUES(?,?,?) "
            "ON CONFLICT(library_id, version) DO NOTHING",
            (library_id, version, 1 if registry_confirmed else 0),
        )
        self._conn.execute(
            "UPDATE versions SET registry_confirmed=? WHERE library_id=? AND version=?",
            (1 if registry_confirmed else 0, library_id, version),
        )
        row = self._conn.execute(
            "SELECT id FROM versions WHERE library_id=? AND version=?", (library_id, version)
        ).fetchone()
        self._conn.commit()
        return library_id, int(row[0])
