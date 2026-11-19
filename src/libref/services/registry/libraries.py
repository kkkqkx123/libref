"""Library and version records: the only place that writes libraries/versions."""

from __future__ import annotations

from typing import Any

from libref.services.infra.timestamps import iso_utc
from libref.text.normalize import normalize_markdown


def mining_version_ids(
    conn,
    library: str | None = None,
    version: str | None = None,
    ecosystem: str | None = None,
    tenant_id: str | None = None,
) -> list[int]:
    """Resolve library filters to version ids inside one tenant when given."""
    from libref.services.infra.tenant import (
        is_global,
        resolve_version_id,
        table_has_tenant,
    )

    scoped = tenant_id not in (None, "*") and not is_global(str(tenant_id or ""))
    if scoped:
        has_lib = table_has_tenant(conn, "libraries")
        has_ver = table_has_tenant(conn, "versions")
        if not has_lib and not has_ver:
            scoped = False
    if scoped and library is not None and version is not None:
        assert tenant_id is not None
        try:
            vid = resolve_version_id(conn, str(tenant_id), library, version, ecosystem)
        except ValueError:
            return []
        return [int(vid)]
    filters: list[str] = []
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
    if scoped:
        assert tenant_id is not None
        from libref.services.infra.tenant import table_has_tenant as _has

        if _has(conn, "libraries"):
            filters.append("l.tenant_id = ?")
            params.append(str(tenant_id))
        if _has(conn, "versions"):
            filters.append("v.tenant_id = ?")
            params.append(str(tenant_id))
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = conn.execute(
        f"SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id {where} ORDER BY v.id",
        params,
    ).fetchall()
    return [int(r[0]) for r in rows]


class LibraryService:
    """Library and version catalog reads and ensure-or-create writes."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def _tenant_scoped(self, tenant_id: str | None, table: str) -> str | None:
        from libref.services.infra.tenant import is_global, table_has_tenant

        if tenant_id is None or tenant_id == "*":
            return None
        if is_global(str(tenant_id)):
            return None
        if not table_has_tenant(self._conn, table):
            return None
        return str(tenant_id)

    def list_libraries(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        scoped = self._tenant_scoped(tenant_id, "libraries")
        if scoped is None:
            rows = self._conn.execute(
                "SELECT l.id, l.ecosystem, l.name, l.description, l.homepage, l.docs_url, "
                "l.repo_url, l.resolved_from, l.resolved_at, "
                "(SELECT count(*) FROM versions v WHERE v.library_id = l.id) AS versions "
                "FROM libraries l ORDER BY l.ecosystem, l.name"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT l.id, l.ecosystem, l.name, l.description, l.homepage, l.docs_url, "
                "l.repo_url, l.resolved_from, l.resolved_at, "
                "(SELECT count(*) FROM versions v WHERE v.library_id = l.id AND v.tenant_id = ?) AS versions "
                "FROM libraries l WHERE l.tenant_id = ? ORDER BY l.ecosystem, l.name",
                (scoped, scoped),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            raw = r[3] if len(r) > 3 else None
            out.append(
                {
                    "id": r[0],
                    "ecosystem": r[1],
                    "name": r[2],
                    "description": normalize_markdown(raw) if raw else raw,
                    "homepage": r[4],
                    "docs_url": r[5],
                    "repo_url": r[6],
                    "resolved_from": r[7],
                    "resolved_at": iso_utc(r[8]),
                    "versions": r[9],
                }
            )
        return out

    def record_site_version(
        self,
        version_id: int,
        value: str,
        signal: str = "",
        sample_url: str = "",
    ) -> None:
        """Persist what the site states it documents; pure write, no comparison."""
        from libref.db.conn import write_lock

        guard = write_lock(self._conn)
        guard.acquire()
        try:
            self._conn.execute(
                "UPDATE versions SET site_version=?, site_version_signal=?,"
                " site_version_sample=? WHERE id=?",
                (value, signal, sample_url, version_id),
            )
            self._conn.commit()
        finally:
            guard.release()

    def site_version(self, version_id: int) -> dict[str, str]:
        row = self._conn.execute(
            "SELECT site_version, site_version_signal, site_version_sample"
            " FROM versions WHERE id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            return {"value": "", "signal": "", "sample_url": ""}
        return {
            "value": str(row[0] or ""),
            "signal": str(row[1] or ""),
            "sample_url": str(row[2] or ""),
        }

    def list_versions(
        self,
        library: str,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        scoped = self._tenant_scoped(tenant_id, "versions")
        lib_scoped = self._tenant_scoped(tenant_id, "libraries")
        extra = ""
        params: list[Any] = [library]
        if ecosystem is not None:
            extra += " AND l.ecosystem = ?"
            params.append(ecosystem)
        if lib_scoped is not None:
            extra += " AND l.tenant_id = ?"
            params.append(lib_scoped)
        if scoped is not None:
            extra += " AND v.tenant_id = ?"
            params.append(scoped)
        rows = self._conn.execute(
            "SELECT v.id, v.version, v.registry_confirmed, v.created_at, l.ecosystem, "
            "(SELECT count(*) FROM pages p WHERE p.version_id = v.id) AS pages "
            "FROM versions v JOIN libraries l ON l.id = v.library_id "
            f"WHERE l.name = ?{extra} ORDER BY v.created_at",
            params,
        ).fetchall()
        from libref.crawler.registry import is_prerelease

        return [
            {
                "id": r[0],
                "version": r[1],
                "registry_confirmed": bool(r[2]),
                # Catalog consumers (webui) cannot classify pre-releases on their
                # own without duplicating the per-registry rules, so flag it here.
                "prerelease": is_prerelease(r[1], r[4]),
                "created_at": iso_utc(r[3]),
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
        tenant_id: str = "default",
    ) -> tuple[int, int]:
        """Ensure library/version rows exist, return (library_id, version_id)."""
        from libref.db.conn import write_lock

        guard = write_lock(self._conn)
        guard.acquire()
        try:
            return self._ensure_locked(
                library,
                version,
                ecosystem,
                homepage=homepage,
                docs_url=docs_url,
                repo_url=repo_url,
                resolved_from=resolved_from,
                registry_confirmed=registry_confirmed,
                tenant_id=tenant_id,
            )
        finally:
            guard.release()

    def _ensure_locked(
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
        tenant_id: str = "default",
    ) -> tuple[int, int]:
        from libref.services.infra.tenant import normalize_tenant, table_has_tenant

        tenant = normalize_tenant(tenant_id)
        has_lib = table_has_tenant(self._conn, "libraries")
        has_ver = table_has_tenant(self._conn, "versions")
        if has_lib:
            self._conn.execute(
                "INSERT INTO libraries(ecosystem, name, tenant_id) VALUES(?,?,?) "
                "ON CONFLICT(ecosystem, name) DO NOTHING",
                (ecosystem, library, tenant),
            )
        else:
            self._conn.execute(
                "INSERT INTO libraries(ecosystem, name) VALUES(?,?) "
                "ON CONFLICT(ecosystem, name) DO NOTHING",
                (ecosystem, library),
            )
        row = self._conn.execute(
            "SELECT id FROM libraries WHERE ecosystem=? AND name=?",
            (ecosystem, library),
        ).fetchone()
        library_id = int(row[0])
        if (
            homepage is not None
            or docs_url is not None
            or repo_url is not None
            or resolved_from is not None
        ):
            self._conn.execute(
                "UPDATE libraries SET homepage=COALESCE(?, homepage), docs_url=COALESCE(?, docs_url), "
                "repo_url=COALESCE(?, repo_url), resolved_from=COALESCE(?, resolved_from), "
                "resolved_at=datetime('now') WHERE id=?",
                (homepage, docs_url, repo_url, resolved_from, library_id),
            )
        if has_ver:
            self._conn.execute(
                "INSERT INTO versions(library_id, version, registry_confirmed, tenant_id)"
                " VALUES(?,?,?,?) "
                "ON CONFLICT(library_id, version) DO NOTHING",
                (library_id, version, 1 if registry_confirmed else 0, tenant),
            )
        else:
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
            "SELECT id FROM versions WHERE library_id=? AND version=?",
            (library_id, version),
        ).fetchone()
        self._conn.commit()
        return library_id, int(row[0])

    def catalog(
        self,
        library: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Unified catalog: libraries when no name is given, versions otherwise."""
        if library is None:
            return self.list_libraries(tenant_id=tenant_id)
        return self.list_versions(library, ecosystem, tenant_id=tenant_id)
