"""Source code ingestion: clone repos, extract calls, persist entities and chunks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from libref.services.code.repo_chunks import _store_source_chunks, _sweep_source_pages
from libref.services.code.repo_collect import (
    _collect_decl_index,
    _collect_repo,
    _load_raw_batch,
)
from libref.services.code.repo_ids import call_uid, entity_uid
from libref.services.code.repo_persist import (
    _check_manifest_identity,
    _persist_entities,
    _persist_repo_deps,
    _sweep_calls,
    _upsert_calls,
)
from libref.services.code.repo_source import (
    list_repo_dependencies,
    lookup_index_source_cached,
)

logger = logging.getLogger(__name__)

INDEX_REPO_JOB_KIND = "index_repo"


def _same_source(left: str | None, right: str | None) -> bool:
    """Source identity ignoring a git prefix and trailing slashes."""

    def _norm(value: str | None) -> str:
        return (value or "").strip().removeprefix("git+").rstrip("/")

    both = (_norm(left), _norm(right))
    return bool(both[0]) and both[0] == both[1]


__all__ = [
    "INDEX_REPO_JOB_KIND",
    "_collect_decl_index",
    "_collect_repo",
    "_load_raw_batch",
    "_persist_entities",
    "_store_source_chunks",
    "_sweep_calls",
    "_sweep_source_pages",
    "_upsert_calls",
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
    max_files: int | None = None,
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
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            max_files=max_files,
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
    max_files: int | None = None,
) -> dict[str, int]:
    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import resolution as ast_resolution
    from libref.astminer.repos import RepoManager, iter_source_files
    from libref.services.infra.progress import (
        CODE_KIND,
        resume_offset,
        save_progress,
    )
    from libref.services.registry.libraries import LibraryService

    settings_dir = Path(repos_dir)
    if not source:
        hit = lookup_index_source_cached(conn, ecosystem, library, version, settings)
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
        if not _same_source(old_url, source) and not _same_source(
            str(source), str(old_path or "")
        ):
            raise ValueError(
                f"version already indexed from different source: {old_url}"
            )
    root, commit = manager.ensure(conn, repo_name, source, update=update)
    conn.commit()
    files = [r for r in iter_source_files(root, ast_config.all_suffixes())]
    prefix = (subdir or "").strip().strip("/")
    if prefix:
        files = [r for r in files if r == prefix or r.startswith(prefix + "/")]
    if only_files:
        wanted_files = set(only_files)
        files = [r for r in files if r in wanted_files]
    files_truncated = False
    if max_files is not None and len(files) > max_files:
        files = files[:max_files]
        files_truncated = True
    batch_size = max(1, int(getattr(settings, "index_batch_files", 200) or 200))
    done, fingerprint = resume_offset(conn, version_id, CODE_KIND, files, fresh=fresh)
    counts = {"resumed_skipped": done, "batches": 0}
    counts["files_truncated"] = 1 if files_truncated else 0
    all_decls, file_aliases, collect_counts, live = _collect_decl_index(
        conn,
        repo_name,
        root,
        files,
    )
    counts.update(collect_counts)
    counts["evicted"] = ast_cache.sweep_repo(conn, repo_name, live)
    conn.commit()
    n_entities, swept_entities = _persist_entities(
        conn, library_id, version_id, all_decls
    )
    counts["entities"] = n_entities
    counts["swept_entities"] = swept_entities
    conn.commit()
    remaining = files[done:]
    wanted_calls: set[str] = set()
    n_calls = 0
    removed_ids: list[int] = []
    inserted_ids: list[int] = []
    n_chunks = 0
    chunk_dropped = 0
    for start in range(0, len(remaining), batch_size):
        batch_files = remaining[start : start + batch_size]
        batch_aliases = {rel: file_aliases.get(rel, []) for rel in batch_files}
        batch_raw = _load_raw_batch(conn, repo_name, root, batch_files)
        resolved = ast_resolution.resolve_calls(
            all_decls, batch_aliases, batch_raw, repo_name, commit
        )
        batch_count, batch_wanted = _upsert_calls(
            conn, library_id, version_id, resolved, commit
        )
        n_calls += batch_count
        wanted_calls.update(batch_wanted)
        conn.commit()
        batch_live = {rel: live[rel] for rel in batch_files if rel in live}
        batch_chunks, batch_dropped, batch_removed, batch_inserted = (
            _store_source_chunks(
                conn,
                repo_name,
                root,
                sorted(batch_live),
                all_decls,
                library_id,
                version_id,
            )
        )
        n_chunks += batch_chunks
        chunk_dropped += batch_dropped
        removed_ids.extend(batch_removed)
        inserted_ids.extend(batch_inserted)
        conn.commit()
        counts["batches"] += 1
        save_progress(
            conn,
            version_id,
            CODE_KIND,
            total=len(files),
            done=done + start + len(batch_files),
            fingerprint=fingerprint,
            batch_size=batch_size,
        )
    swept_calls = _sweep_calls(conn, version_id, wanted_calls)
    counts["calls"] = n_calls
    counts["swept_calls"] = swept_calls
    conn.commit()
    _check_manifest_identity(ecosystem, library, root)
    n_deps = _persist_repo_deps(conn, library_id, version_id, root)
    counts["manifest_deps"] = n_deps
    swept_pages = _sweep_source_pages(conn, repo_name, set(live))
    removed_ids.extend(swept_pages)
    counts["swept_pages"] = len(swept_pages)
    conn.commit()
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
    max_files: int | None = None,
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
            max_files=max_files,
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
    max_files: int | None = None,
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
            max_files=max_files,
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
    max_files: int | None = None,
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
            max_files=max_files,
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
        max_files=max_files,
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
            fresh=bool(p.get("fresh", False)),
            subdir=p.get("subdir"),
            only_files=p.get("only_files"),
            max_files=p.get("max_files"),
        )

    return runner
