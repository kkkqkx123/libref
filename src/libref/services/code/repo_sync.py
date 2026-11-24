"""Vector synchronization and resume cursor writes for source ingestion."""

from __future__ import annotations

from libref.services.infra.progress import CODE_KIND, save_progress


async def sync_vectors(
    conn,
    settings,
    vec_store,
    embed,
    doc_store,
    counts: dict,
    removed_ids: list[int],
    inserted_ids: list[int],
    *,
    version_id: int,
    verify: bool = True,
) -> None:
    """Push changed source documents to the derivable vector index."""
    from libref.services.ops.maintenance import sync_vectors_for_docs

    await sync_vectors_for_docs(
        conn,
        settings,
        vec_store,
        embed,
        doc_store,
        counts,
        removed_ids,
        inserted_ids,
        version_id=version_id,
        verify=verify,
    )


def advance_progress(
    conn,
    version_id: int,
    files: list[str],
    done: int,
    start: int,
    batch_size: int,
    fingerprint: str,
) -> None:
    """Mark a batch complete only after its derived index is in sync."""
    save_progress(
        conn,
        version_id,
        CODE_KIND,
        total=len(files),
        done=done + start + batch_size,
        fingerprint=fingerprint,
        batch_size=batch_size,
    )
