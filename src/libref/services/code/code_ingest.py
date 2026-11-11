"""Source code ingestion: clone repos, extract calls, persist entities and chunks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from libref.services.code.repo_chunks import _store_source_chunks, _sweep_source_pages
from libref.services.code.repo_collect import _collect_repo
from libref.services.code.repo_ids import call_uid, entity_uid
from libref.services.code.repo_persist import (
    _check_manifest_identity,
    _persist_calls,
    _persist_entities,
    _persist_repo_deps,
)
from libref.services.code.repo_source import (
    list_repo_dependencies,
    lookup_index_source_cached,
)

logger = logging.getLogger(__name__)

INDEX_REPO_JOB_KIND = "index_repo"


__all__ = [
    "INDEX_REPO_JOB_KIND",
    "_collect_repo",
    "_persist_calls",
    "_persist_entities",
    "_store_source_chunks",
    "_sweep_source_pages",
    "build_repo_runner",
    "call_uid",
    "entity_uid",
    "index_repo",
    "index_repo_async",
    "index_repo_by_name",
    "index_repo_by_name_async",
    "list_repo_dependencies",
    "lookup_index_source_cached",
]


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    from libref.crawler.http import build_async_client

    async with build_async_client(settings) as client:
        yield client


async def index_repo_async(
    conn,
    library: str,
    version: str,
    source: str | None = None,
    *,
    update: bool = False,
    repos_dir: Path,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
    repos_quota_mb: int | None = None,
    settings=None,
    vec_store=None,
    embed=None,
) -> dict[str, int]:
    """Lock one version across doc and code writers, then run the ingest."""
    from libref.services.infra.version_lock import version_lock

    async with version_lock(conn, ecosystem, library, version):
        return await _index_repo_inner(
            conn,
            library,
            version,
            source,
            update=update,
            repos_dir=repos_dir,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            repos_quota_mb=repos_quota_mb,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
        )


async def _index_repo_inner(
    conn,
    library: str,
    version: str,
    source: str | None = None,
    *,
    update: bool = False,
    repos_dir: Path,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
    repos_quota_mb: int | None = None,
    settings=None,
    vec_store=None,
    embed=None,
) -> dict[str, int]:
    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import resolution as ast_resolution
    from libref.astminer.repos import RepoManager, iter_source_files
    from libref.services.registry.libraries import LibraryService

    settings_dir = Path(repos_dir)
    if not source:
        hit = lookup_index_source_cached(conn, ecosystem, library, version)
        if hit is None:
            raise ValueError(
                "repo source unknown: pass an explicit address or resolve by name "
                "with --ecosystem (needs registry cache or network)"
            )
        source, cached_origin = hit
        if resolved_from is None:
            resolved_from = cached_origin
    if resolved_from is None:
        resolved_from = "explicit"
    catalog = LibraryService(conn)
    library_id, version_id = catalog.ensure_library_version(
        library,
        version,
        ecosystem,
        repo_url=source,
        resolved_from=resolved_from,
        registry_confirmed=registry_confirmed,
    )
    if repos_quota_mb is not None:
        from libref.services.ops.retention import check_quota

        check_quota(settings_dir, int(repos_quota_mb), "repos")
    manager = RepoManager(settings_dir)
    repo_name = f"{ecosystem}/{library}/{version}"
    existing = conn.execute(
        "SELECT url, local_path FROM repos WHERE name=?", (repo_name,)
    ).fetchone()
    if existing is not None:
        old_url, old_path = existing[0], existing[1]
        if old_url != source and str(source) != str(old_path):
            raise ValueError(
                f"version already indexed from different source: {old_url}"
            )
    root, commit = manager.ensure(conn, repo_name, source, update=update)
    conn.commit()
    files = [r for r in iter_source_files(root, ast_config.all_suffixes())]
    all_decls, file_aliases, all_raw, counts, live = _collect_repo(
        conn,
        repo_name,
        root,
        files,
    )
    counts["evicted"] = ast_cache.sweep_repo(conn, repo_name, live)
    conn.commit()
    resolved = ast_resolution.resolve_calls(
        all_decls, file_aliases, all_raw, repo_name, commit
    )
    n_entities, swept_entities = _persist_entities(
        conn, library_id, version_id, all_decls
    )
    n_calls, swept_calls = _persist_calls(
        conn, library_id, version_id, resolved, commit
    )
    _check_manifest_identity(ecosystem, library, root)
    n_deps = _persist_repo_deps(conn, library_id, version_id, root)
    counts["manifest_deps"] = n_deps
    n_chunks, chunk_dropped, removed_ids, inserted_ids = _store_source_chunks(
        conn, repo_name, root, sorted(live), all_decls, library_id, version_id
    )
    swept_pages = _sweep_source_pages(conn, repo_name, set(live))
    removed_ids.extend(swept_pages)
    counts["swept_pages"] = len(swept_pages)
    conn.commit()
    counts["entities"] = n_entities
    counts["calls"] = n_calls
    counts["swept_entities"] = swept_entities
    counts["swept_calls"] = swept_calls
    counts["source_chunks"] = n_chunks
    counts["chunk_empty_dropped"] = chunk_dropped
    counts["vector_failed"] = 0
    counts["chunk_truncated"] = 0
    if settings is not None and vec_store is not None and embed is not None:
        from libref.services.ops.maintenance import sync_vectors_for_docs
        from libref.store.document_store import DocumentStore

        await sync_vectors_for_docs(
            conn,
            settings,
            vec_store,
            embed,
            DocumentStore(conn),
            counts,
            removed_ids,
            inserted_ids,
            version_id=version_id,
        )
    return counts


def index_repo(
    conn,
    library: str,
    version: str,
    source: str | None = None,
    *,
    update: bool = False,
    repos_dir: Path,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
    repos_quota_mb: int | None = None,
    settings=None,
    vec_store=None,
    embed=None,
) -> dict[str, int]:
    return asyncio.run(
        index_repo_async(
            conn,
            library,
            version,
            source,
            update=update,
            repos_dir=repos_dir,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            repos_quota_mb=repos_quota_mb,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
        )
    )


def index_repo_by_name(
    conn,
    settings,
    ecosystem: str | None,
    library: str,
    version: str | None = None,
    *,
    update: bool = False,
    repos_dir=None,
    explicit_source: str | None = None,
    include_prerelease: bool = False,
) -> dict[str, int]:
    return asyncio.run(
        index_repo_by_name_async(
            conn,
            settings,
            ecosystem,
            library,
            version,
            update=update,
            repos_dir=repos_dir,
            explicit_source=explicit_source,
            include_prerelease=include_prerelease,
        )
    )


async def index_repo_by_name_async(
    conn,
    settings,
    ecosystem: str | None,
    library: str,
    version: str | None = None,
    *,
    update: bool = False,
    repos_dir=None,
    explicit_source: str | None = None,
    include_prerelease: bool = False,
) -> dict[str, int]:
    from libref.services.registry.resolver import resolve_repo

    if explicit_source is not None:
        if version is None:
            raise ValueError("explicit repo source requires a version")
        eco = ecosystem.strip().lower() if ecosystem else "custom"
        vec_store = None
        embed = None
        try:
            from libref.services.infra.vectors import build_vector as _build_vector

            vec_store, embed, _ = _build_vector(settings)
        except Exception:  # noqa: BLE001 — vector stack is optional; indexing continues without it
            vec_store, embed = None, None
        return await index_repo_async(
            conn,
            library.strip(),
            version,
            explicit_source,
            update=update,
            repos_dir=Path(repos_dir)
            if repos_dir is not None
            else Path(settings.repos_dir),
            ecosystem=eco,
            resolved_from="explicit",
            registry_confirmed=True,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
        )
    async with _client(settings) as client:
        resolution = await resolve_repo(
            conn,
            client,
            settings,
            ecosystem,
            library,
            version,
            include_prerelease=include_prerelease,
        )
    assert resolution.repo_url is not None
    vec_store = None
    embed = None
    try:
        from libref.services.infra.vectors import build_vector as _build_vector

        vec_store, embed, _ = _build_vector(settings)
    except Exception:  # noqa: BLE001 — vector stack is optional; indexing continues without it
        vec_store, embed = None, None
    return await index_repo_async(
        conn,
        resolution.name,
        resolution.version,
        resolution.repo_url,
        update=update,
        repos_dir=Path(repos_dir)
        if repos_dir is not None
        else Path(settings.repos_dir),
        ecosystem=resolution.ecosystem,
        resolved_from=resolution.source,
        registry_confirmed=resolution.registry_confirmed,
        settings=settings,
        vec_store=vec_store,
        embed=embed,
    )


def build_repo_runner(settings, conn, repos_dir=None, vec_store=None, embed=None):
    """Build a job runner handling repo indexing only; mining lives in mining_refresh."""
    resolved_dir = (
        Path(repos_dir) if repos_dir is not None else Path(settings.repos_dir)
    )

    async def runner(job):
        if job.kind != INDEX_REPO_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        local_vec, local_embed = vec_store, embed
        if local_vec is None or local_embed is None:
            try:
                from libref.services.infra.vectors import build_vector as _build_vector

                built_vec, built_embed, _ = _build_vector(settings)
                if local_vec is None:
                    local_vec = built_vec
                if local_embed is None:
                    local_embed = built_embed
            except Exception:  # noqa: BLE001, S110 — vector stack is optional; indexing continues without it
                pass
        return await index_repo_async(
            conn,
            p["library"],
            p["version"],
            p.get("source"),
            update=bool(p.get("update", False)),
            repos_dir=resolved_dir,
            ecosystem=str(p.get("ecosystem", "custom") or "custom"),
            resolved_from=p.get("resolved_from"),
            registry_confirmed=bool(p.get("registry_confirmed", True)),
            repos_quota_mb=int(settings.repos_quota_mb),
            settings=settings,
            vec_store=local_vec,
            embed=local_embed,
        )

    return runner
