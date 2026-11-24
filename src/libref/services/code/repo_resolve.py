"""Batch call resolution, source chunking and progress reporting."""

from __future__ import annotations

from typing import Any

from libref.astminer.resolution import resolve_calls
from libref.services.code.repo_chunks import _store_source_chunks
from libref.services.code.repo_collect import _load_raw_batch
from libref.services.code.repo_persist import _upsert_calls
from libref.services.code.repo_sync import advance_progress, sync_vectors


async def process_repo_batches(
    conn,
    *,
    library_id: int,
    version_id: int,
    files: list[str],
    done: int,
    batch_size: int,
    repo_name: str,
    root,
    commit: str,
    source_id: int,
    resolution_decls: list,
    file_aliases: dict[str, list],
    all_decls: list,
    live: dict,
    settings,
    source_role: str,
    origin_kind: str,
    effective_names: set[str],
    target_prefixes: tuple[str, ...],
    source_status: str,
    filt: dict,
    code_search: bool,
    vectors_live: bool,
    vec_store,
    embed,
    doc_store,
    counts: dict[str, Any],
    wanted_calls: set[str],
    fingerprint: str,
    beat,
) -> dict[str, Any]:
    """Resolve and persist one batch at a time, keeping resume and vector order."""
    remaining = files[done:]
    n_calls = 0
    removed_ids: list[int] = []
    inserted_ids: list[int] = []
    n_chunks = 0
    chunk_dropped = 0
    for start in range(0, len(remaining), batch_size):
        batch_files = remaining[start : start + batch_size]
        batch_aliases = {rel: file_aliases.get(rel, []) for rel in batch_files}
        batch_raw = _load_raw_batch(conn, repo_name, root, batch_files)
        resolved = resolve_calls(
            resolution_decls,
            batch_aliases,
            batch_raw,
            repo_name,
            commit,
            source_id=source_id,
            sample_prefix=repo_name,
            source_role=source_role,
            origin_kind=origin_kind,
            target_names=effective_names,
            target_prefixes=target_prefixes,
            source_target_status=source_status,
            strict_target_status=bool(filt.get("require_resolved_targets")),
            stdlib_filter_enabled=bool(
                getattr(settings, "stdlib_filter_enabled", True)
            ),
            stdlib_table_version=str(getattr(settings, "stdlib_table_version", "1")),
        )
        batch_count, batch_wanted = _upsert_calls(
            conn, library_id, version_id, resolved, commit
        )
        n_calls += batch_count
        wanted_calls.update(batch_wanted)
        conn.commit()
        if code_search:
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
        if vectors_live:
            await sync_vectors(
                conn,
                settings,
                vec_store,
                embed,
                doc_store,
                counts,
                batch_removed,
                batch_inserted,
                version_id=version_id,
                verify=False,
            )
        # Sync-then-mark like the docs path: a crash never advances the
        # cursor past unsynced vectors, so resume replays the batch.
        advance_progress(
            conn, version_id, files, done, start, len(batch_files), fingerprint
        )
        beat()

    return {
        "calls": n_calls,
        "wanted_calls": wanted_calls,
        "removed_ids": removed_ids,
        "inserted_ids": inserted_ids,
        "chunks": n_chunks,
        "chunk_dropped": chunk_dropped,
    }
