"""Source code ingestion: clone repos, extract calls, persist entities and chunks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from libref.services.code.repo_chunks import _sweep_source_pages
from libref.services.code.repo_ids import call_uid, entity_uid
from libref.services.code.repo_persist import (
    _check_manifest_identity,
    _persist_entities,
    _persist_export_aliases,
    _persist_import_edges,
    _persist_repo_deps,
    _sweep_calls,
)
from libref.services.code.repo_prepare import prepare_repo_index
from libref.services.code.repo_resolve import process_repo_batches
from libref.services.code.repo_source import (
    list_repo_dependencies,
    lookup_index_source_cached,
)
from libref.services.code.repo_sync import sync_vectors
from libref.services.infra.jobs import INDEX_REPO_KIND

logger = logging.getLogger(__name__)

INDEX_REPO_JOB_KIND = INDEX_REPO_KIND


__all__ = [
    "INDEX_REPO_JOB_KIND",
    "_sweep_calls",
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
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    """Lock one version across doc and code writers, then run the ingest."""
    from libref.services.infra.version_lock import version_lock

    _stale_after = float(getattr(settings, "heartbeat_timeout", 600.0) or 600.0)
    from libref.services.infra.tenant import normalize_tenant as _ntc

    tenant_id = _ntc(tenant_id)
    async with version_lock(
        conn, ecosystem, library, version, stale_after=_stale_after, tenant_id=tenant_id
    ):
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
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
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
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    prepared = prepare_repo_index(
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
        fresh=fresh,
        subdir=subdir,
        only_files=only_files,
        exclude_patterns=exclude_patterns,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        heartbeat=heartbeat,
        tenant_id=tenant_id,
        source_role=source_role,
        origin_kind=origin_kind,
        target_filter=target_filter,
        warm_ast=warm_ast,
    )
    library_id = prepared["library_id"]
    version_id = prepared["version_id"]
    source = prepared["source"]
    source_id = prepared["source_id"]
    counts = prepared["counts"]
    _beat = prepared["beat"]
    repo_name = prepared["repo_name"]
    root = prepared["root"]
    commit = prepared["commit"]
    files = prepared["files"]
    batch_size = prepared["batch_size"]
    done = prepared["done"]
    fingerprint = prepared["fingerprint"]
    code_search = prepared["code_search"]
    _vectors_live = prepared["vectors_live"]
    _doc_store = prepared["doc_store"]
    all_decls = prepared["all_decls"]
    file_aliases = prepared["file_aliases"]
    export_facts = prepared["export_facts"]
    live = prepared["live"]
    persist_definitions = code_search
    filt = target_filter or {}
    explicit_names = {str(v) for v in filt.get("names", []) if str(v).strip()}
    target_prefixes = tuple(str(v) for v in filt.get("prefixes", []) if str(v).strip())
    from libref.services.code.usage_sources import source_target_status

    if persist_definitions:
        n_entities, swept_entities = _persist_entities(
            conn, library_id, version_id, all_decls
        )
        counts["import_edges"] = _persist_import_edges(
            conn, library_id, version_id, file_aliases
        )
        counts["export_aliases"] = _persist_export_aliases(
            conn, version_id, export_facts, tenant_id=tenant_id
        )
    else:
        n_entities, swept_entities = 0, 0
        counts["import_edges"] = 0
        counts["export_aliases"] = 0
    counts["entities"] = n_entities
    counts["swept_entities"] = swept_entities
    conn.commit()
    resolution_decls = all_decls
    if persist_definitions:
        target_names = {
            str(r[0])
            for r in conn.execute(
                "SELECT qualified_name FROM api_entities WHERE version_id=?",
                (version_id,),
            ).fetchall()
        }
    else:
        from libref.services.code.usage_ingest import target_declarations

        target_decls = target_declarations(
            conn,
            version_id,
            names=explicit_names or None,
            prefixes=None if explicit_names else target_prefixes,
        )
        resolution_decls = all_decls + target_decls
        target_names = {decl.qualified_name for decl in target_decls}
    source_status = source_target_status(
        source_role,
        filt.get("declared_library_version"),
        version,
        version_required=bool(filt.get("version_required")),
    )
    effective_names = explicit_names or target_names
    wanted_calls: set[str] = set()
    if done > 0:
        # Resume skips the done files, so their call identities must seed the
        # sweep set; otherwise the final sweep mistakes them for deleted calls.
        # Content drift inside the skipped range needs a fresh run.
        wanted_calls.update(
            str(r[0])
            for r in conn.execute(
                "SELECT call_uid FROM call_instances WHERE version_id=?",
                (version_id,),
            ).fetchall()
        )
    batch_result = await process_repo_batches(
        conn,
        library_id=library_id,
        version_id=version_id,
        files=files,
        done=done,
        batch_size=batch_size,
        repo_name=repo_name,
        root=root,
        commit=commit,
        source_id=source_id,
        resolution_decls=resolution_decls,
        file_aliases=file_aliases,
        all_decls=all_decls,
        live=live,
        settings=settings,
        source_role=source_role,
        origin_kind=origin_kind,
        effective_names=effective_names,
        target_prefixes=target_prefixes,
        source_status=source_status,
        filt=filt,
        code_search=code_search,
        vectors_live=_vectors_live,
        vec_store=vec_store,
        embed=embed,
        doc_store=_doc_store,
        counts=counts,
        wanted_calls=wanted_calls,
        fingerprint=fingerprint,
        beat=_beat,
    )
    n_calls = batch_result["calls"]
    wanted_calls = batch_result["wanted_calls"]
    removed_ids = batch_result["removed_ids"]
    n_chunks = batch_result["chunks"]
    chunk_dropped = batch_result["chunk_dropped"]
    swept_calls = _sweep_calls(conn, version_id, wanted_calls, source_id=source_id)
    counts["calls"] = n_calls
    counts["swept_calls"] = swept_calls
    conn.commit()
    _check_manifest_identity(ecosystem, library, root)
    n_deps = _persist_repo_deps(conn, library_id, version_id, root)
    counts["manifest_deps"] = n_deps
    swept_pages = _sweep_source_pages(conn, repo_name, set(live)) if code_search else []
    removed_ids.extend(swept_pages)
    counts["swept_pages"] = len(swept_pages)
    conn.commit()
    counts["source_chunks"] = n_chunks
    counts["chunk_empty_dropped"] = chunk_dropped
    counts["chunk_truncated"] = 0
    if _vectors_live:
        await sync_vectors(
            conn,
            settings,
            vec_store,
            embed,
            _doc_store,
            counts,
            swept_pages,
            [],
            version_id=version_id,
        )
    from libref.services.infra.progress import CODE_KIND as _CODE_KIND
    from libref.services.infra.progress import clear_progress as _clear_progress

    _clear_progress(conn, version_id, _CODE_KIND)
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
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
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
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
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
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
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
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
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
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    from libref.services.registry.resolver import resolve_repo

    if explicit_source is not None:
        if version is None:
            raise ValueError("explicit repo source requires a version")
        if ecosystem is not None:
            raise ValueError(
                "explicit source and ecosystem resolution are mutually exclusive"
            )
        eco = "custom"
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
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
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
        fresh=fresh,
        subdir=subdir,
        only_files=only_files,
        exclude_patterns=exclude_patterns,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        heartbeat=heartbeat,
        tenant_id=tenant_id,
        source_role=source_role,
        origin_kind=origin_kind,
        target_filter=target_filter,
    )


def build_repo_runner(
    settings, conn, repos_dir=None, vec_store=None, embed=None, mgr=None
):
    """Build a job runner handling repo indexing only; mining lives in mining_refresh."""
    resolved_dir = (
        Path(repos_dir) if repos_dir is not None else Path(settings.repos_dir)
    )

    async def runner(job):
        if job.kind != INDEX_REPO_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        heartbeat = (lambda: mgr.heartbeat(job.id)) if mgr is not None else None
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
        from libref.services.infra.tenant import normalize_tenant as _ntr

        _t = _ntr(
            (p.get("tenant_id") if isinstance(p, dict) else None)
            or getattr(job, "tenant_id", "default")
            or "default"
        )
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
            tenant_id=_t,
            settings=settings,
            vec_store=local_vec,
            embed=local_embed,
            fresh=bool(p.get("fresh", False)),
            subdir=p.get("subdir"),
            only_files=p.get("only_files"),
            exclude_patterns=p.get("exclude_patterns"),
            max_files=p.get("max_files"),
            heartbeat=heartbeat,
            source_role=p.get("source_role", "implementation"),
            origin_kind=p.get("origin_kind", "repo_file"),
            target_filter=p.get("target_filter"),
            warm_ast=True,
        )

    return runner
