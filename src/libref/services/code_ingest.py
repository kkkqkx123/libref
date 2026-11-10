"""Source code ingestion: clone repos, extract calls, persist entities and chunks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

INDEX_REPO_JOB_KIND = "index_repo"


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    from libref.crawler.http import build_async_client

    async with build_async_client(settings) as client:
        yield client


def entity_uid(version_id: int, qualified_name: str, kind: str) -> str:
    import hashlib

    blob = f"{version_id}\0{qualified_name}\0{kind}".encode()
    return hashlib.sha1(blob).hexdigest()


def call_uid(
    repo: str,
    caller_file: str,
    line_start: int,
    line_end: int,
    callee: str,
    args: list[str],
) -> str:
    import hashlib

    blob = "\0".join(
        [repo, caller_file, str(line_start), str(line_end), callee, "\x1f".join(args)]
    ).encode()
    return hashlib.sha1(blob).hexdigest()


def _collect_repo(
    conn,
    repo_name: str,
    root,
    files: list[str],
) -> tuple[
    list,
    dict[str, list[tuple[str, str]]],
    list,
    dict[str, int],
    dict[str, tuple[str, int]],
    dict[str, bytes],
]:
    import hashlib
    from pathlib import Path

    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine

    all_decls: list = []
    file_aliases: dict[str, list[tuple[str, str]]] = {}
    all_raw: list = []
    counts = {"files": 0, "parsed": 0, "cached": 0}
    live: dict[str, tuple[str, int]] = {}
    sources: dict[str, bytes] = {}
    for rel in files:
        counts["files"] += 1
        cfg = ast_config.config_for_suffix(Path(rel).suffix)
        if cfg is None:
            continue
        fp = ast_config.grammar_fingerprint(cfg)
        live[rel] = (fp, cfg.config_version)
        data = (root / rel).read_bytes()
        sources[rel] = data
        sha = hashlib.sha1(data).hexdigest()
        hit = ast_cache.lookup(conn, repo_name, rel, fp, cfg.config_version)
        if hit is not None and hit[0] == sha:
            counts["cached"] += 1
            decls, aliases, raw_calls = ast_cache.decode_records(hit[1], rel)
            all_decls.extend(decls)
            file_aliases[rel] = aliases
            all_raw.extend(raw_calls)
            continue
        counts["parsed"] += 1
        decls, aliases, raw_calls = ast_engine.extract_file(rel, cfg, data)
        all_decls.extend(decls)
        file_aliases[rel] = list(aliases)
        all_raw.extend(raw_calls)
        ast_cache.store(
            conn,
            repo_name,
            rel,
            sha,
            fp,
            cfg.config_version,
            ast_cache.encode_records(decls, aliases, raw_calls),
        )
    return all_decls, file_aliases, all_raw, counts, live, sources


def _store_source_chunks(
    conn,
    repo_name: str,
    sources: dict[str, bytes],
    all_decls,
    library_id: int,
    version_id: int,
    budgets=None,
) -> tuple[int, int, list[int], list[int]]:
    """Chunk source code and store it: assemble grouped by file, reuse the documents table for storing; returns chunk count, empty chunks, delete/insert ids."""
    from libref.astminer import grouping as _grouping
    from libref.astminer import source_chunk as _source_chunk
    from libref.chunking import default_budgets
    from libref.store.document_store import DocumentStore

    resolved = budgets or default_budgets()
    by_file: dict[str, list] = {}
    for decl in all_decls:
        by_file.setdefault(decl.file, []).append(decl)
    store = DocumentStore(conn)
    n_chunks = 0
    dropped = 0
    removed_all: list[int] = []
    inserted_all: list[int] = []
    for rel in sorted(sources):
        decls = by_file.get(rel, [])
        if not decls:
            continue
        data = sources[rel]
        groups = _grouping.build_groups(decls, data, rel)
        by_qual = {d.qualified_name: d for d in decls}
        split = _source_chunk.build_source_chunks(
            groups, by_qual, data, rel, budgets=resolved, repo=repo_name
        )
        dropped += split.empty_dropped
        page_url = f"repo://{repo_name}/{rel}"
        raw_chunks = [_source_chunk.as_chunk(c, page_url) for c in split.chunks]
        if not raw_chunks:
            continue
        for order, chunk in enumerate(raw_chunks):
            chunk.sort_order = order
        from libref.store.document_store import DocumentInput

        inputs = [
            DocumentInput(
                url=c.url,
                path=c.path,
                title=c.title,
                content=c.content,
                sort_order=c.sort_order,
                chunk_path=getattr(c, "chunk_path", "embedding"),
                source_group_id=getattr(c, "source_group_id", ""),
                chunk_id=getattr(c, "chunk_id", ""),
                span_kind=getattr(c, "span_kind", "document_range"),
                merged_from=list(getattr(c, "merged_from", []) or []),
                overlap_from=getattr(c, "overlap_from", None),
                content_hash=getattr(c, "content_hash", ""),
                keywords=list(getattr(c, "keywords", []) or []),
                content_type=getattr(c, "content_type", "text/markdown"),
                start_char=getattr(c, "start_char", -1),
                end_char=getattr(c, "end_char", -1),
                prev_chunk_id=getattr(c, "prev_chunk_id", None),
                next_chunk_id=getattr(c, "next_chunk_id", None),
                entities=list(getattr(c, "entities", []) or []),
            )
            for c in raw_chunks
        ]
        result = store.store_page(
            version_id,
            library_id,
            page_url,
            "stored",
            chunks=inputs,
            raw_content=data.decode("utf-8", "replace"),
        )
        removed_all.extend(result.removed_ids)
        inserted_all.extend(result.inserted_ids)
        n_chunks += len(inputs)
    return n_chunks, dropped, removed_all, inserted_all


def _persist_entities(
    conn, library_id: int, version_id: int, all_decls
) -> tuple[int, int]:
    import json as _decl_json

    pending: dict[tuple[str, str], Any] = {}
    for decl in all_decls:
        pending[(decl.qualified_name, decl.kind)] = decl
    for qualified_name, kind in list(pending):
        decl = pending[(qualified_name, kind)]
        uid = entity_uid(version_id, qualified_name, kind)
        conn.execute(
            "INSERT INTO api_entities(entity_uid, library_id, version_id,"
            " qualified_name, kind, parent, signature, params, bases,"
            " start_byte, end_byte, depth) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(entity_uid) DO UPDATE SET parent=excluded.parent,"
            " signature=excluded.signature, params=excluded.params, bases=excluded.bases,"
            " start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            " depth=excluded.depth",
            (
                uid,
                library_id,
                version_id,
                qualified_name,
                kind,
                decl.parent,
                decl.signature,
                _decl_json.dumps(list(decl.params), ensure_ascii=False),
                _decl_json.dumps(list(decl.bases), ensure_ascii=False),
                decl.start_byte,
                decl.end_byte,
                decl.depth,
            ),
        )
    wanted = {entity_uid(version_id, q, k) for q, k in pending}
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        dead = conn.execute(
            f"SELECT entity_uid FROM api_entities WHERE version_id=?"
            f" AND entity_uid NOT IN ({placeholders})",
            (version_id, *sorted(wanted)),
        ).fetchall()
        if dead:
            conn.execute(
                f"DELETE FROM api_entities WHERE version_id=?"
                f" AND entity_uid NOT IN ({placeholders})",
                (version_id, *sorted(wanted)),
            )
    else:
        dead = conn.execute(
            "SELECT entity_uid FROM api_entities WHERE version_id=?", (version_id,)
        ).fetchall()
        if dead:
            conn.execute("DELETE FROM api_entities WHERE version_id=?", (version_id,))
    return len(pending), len(dead)


def _persist_calls(
    conn, library_id: int, version_id: int, resolved, commit_sha: str = ""
) -> tuple[int, int]:
    import json as _uid_json

    entity_ids = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT qualified_name || '|' || kind, id FROM api_entities"
            " WHERE version_id=?",
            (version_id,),
        )
    }
    kinds = ("function", "method", "class", "interface", "enum", "constructor")
    wanted: set[str] = set()
    for call in resolved:
        args_json = _uid_json.dumps(list(call.args), ensure_ascii=False)
        uid = call_uid(
            call.repo,
            call.caller_file,
            call.line_start,
            call.line_end,
            call.callee,
            list(call.args),
        )
        wanted.add(uid)
        ent_id = None
        for kind in kinds:
            ent_id = entity_ids.get(f"{call.callee}|{kind}")
            if ent_id is not None:
                break
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_entity_id, callee_name, args,"
            " repo, caller_file, line_start, line_end, confidence,"
            " library_id, version_id, caller, receiver, func, argument_count,"
            " call_order, start_byte, end_byte, commit_sha)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(call_uid) DO UPDATE SET callee_entity_id=excluded.callee_entity_id,"
            " callee_name=excluded.callee_name, args=excluded.args,"
            " confidence=excluded.confidence, library_id=excluded.library_id,"
            " version_id=excluded.version_id, caller=excluded.caller,"
            " receiver=excluded.receiver, func=excluded.func,"
            " argument_count=excluded.argument_count, call_order=excluded.call_order,"
            " start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            " commit_sha=excluded.commit_sha",
            (
                uid,
                ent_id,
                call.callee,
                args_json,
                call.repo,
                call.caller_file,
                call.line_start,
                call.line_end,
                call.confidence,
                library_id,
                version_id,
                call.caller,
                call.receiver,
                call.func,
                call.argument_count,
                call.call_order,
                call.start_byte,
                call.end_byte,
                commit_sha or call.commit_sha,
            ),
        )
        needs_review = call.confidence == "AMBIGUOUS" or (
            call.confidence == "INFERRED" and ent_id is None
        )
        if needs_review:
            row = conn.execute(
                "SELECT id FROM call_instances WHERE call_uid=?", (uid,)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT INTO review_status(entity_type, entity_id, status)"
                    " VALUES('call_instance', ?, 'pending')"
                    " ON CONFLICT(entity_type, entity_id) DO NOTHING",
                    (int(row[0]),),
                )
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        dead_rows = conn.execute(
            f"SELECT id, call_uid FROM call_instances WHERE version_id=?"
            f" AND call_uid NOT IN ({placeholders})",
            (version_id, *sorted(wanted)),
        ).fetchall()
    else:
        dead_rows = conn.execute(
            "SELECT id, call_uid FROM call_instances WHERE version_id=?", (version_id,)
        ).fetchall()
    for dead_id, _ in dead_rows:
        conn.execute(
            "DELETE FROM review_status WHERE entity_type='call_instance' AND entity_id=?",
            (int(dead_id),),
        )
    if dead_rows:
        dead_uids = sorted(uid for _, uid in dead_rows)
        placeholders = ",".join("?" for _ in dead_uids)
        conn.execute(
            f"DELETE FROM call_instances WHERE version_id=?"
            f" AND call_uid IN ({placeholders})",
            (version_id, *dead_uids),
        )
    return len(resolved), len(dead_rows)


def lookup_index_source_cached(
    conn, ecosystem: str, library: str, version: str
) -> tuple[str, str] | None:
    from pathlib import Path

    repo_name = f"{ecosystem}/{library}/{version}"
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
        (ecosystem, library),
    ).fetchone()
    if lib_row is not None and lib_row[0]:
        return str(lib_row[0]), "libraries"
    if ecosystem in ("pypi", "npm", "maven"):
        try:
            from libref.crawler.registry import normalize_name
            from libref.services.resolver import read_cache

            norm = normalize_name(ecosystem, library)
            cached = read_cache(conn, ecosystem, norm)
        except ValueError:
            cached = None
        if cached is not None and cached.get("repo_url"):
            return str(cached["repo_url"]), "registry-cache"
    return None


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
    from libref.services.version_lock import version_lock

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
    from libref.services.libraries import LibraryService

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
        from libref.services.retention import check_quota

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
    all_decls, file_aliases, all_raw, counts, live, sources = _collect_repo(
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
    n_chunks, chunk_dropped, removed_ids, inserted_ids = _store_source_chunks(
        conn, repo_name, sources, all_decls, library_id, version_id
    )
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
        from libref.services.maintenance import sync_vectors_for_docs
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
) -> dict[str, int]:
    from libref.services.resolver import resolve_repo

    if explicit_source is not None:
        if version is None:
            raise ValueError("explicit repo source requires a version")
        eco = ecosystem.strip().lower() if ecosystem else "custom"
        vec_store = None
        embed = None
        try:
            from libref.services.factory import build_vector as _build_vector

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
        )
    assert resolution.repo_url is not None
    vec_store = None
    embed = None
    try:
        from libref.services.factory import build_vector as _build_vector

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
                from libref.services.factory import build_vector as _build_vector

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
