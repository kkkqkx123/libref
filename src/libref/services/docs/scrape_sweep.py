"""Absent sweeping and vector sync after the source-of-truth store."""

from __future__ import annotations

import logging

from libref.store.document_store import DocumentStore

logger = logging.getLogger(__name__)


def sweep_absent(
    conn,
    store: DocumentStore,
    library_id: int,
    version_id: int,
    seed,
    accepted_urls: list[str],
    *,
    truncated: bool,
    counts: dict[str, int],
    snapshot_ctx: tuple[str, str, str] | None = None,
) -> list[int]:
    if seed.source == "none":
        return []
    if seed.failed or truncated:
        return []
    removed: list[int] = []
    removed_urls: list[str] = []
    discovered = set(accepted_urls)
    for url in store.list_page_urls(version_id):
        if not url.startswith(("http://", "https://")):
            continue
        if url not in discovered:
            result = store.store_page(version_id, library_id, url, "absent")
            removed.extend(result.removed_ids)
            removed_urls.append(url)
            counts["absent"] = counts.get("absent", 0) + 1
    if removed_urls and snapshot_ctx is not None:
        try:
            from libref.services.ops.retention import (
                REASON_ABSENT_SWEEP,
                log_deletion,
            )

            ecosystem, library, version = snapshot_ctx
            log_deletion(
                conn,
                library=library,
                version=version,
                ecosystem=ecosystem,
                reason=REASON_ABSENT_SWEEP,
                detail={
                    "urls": removed_urls[:20],
                    "url_count": len(removed_urls),
                    "removed_docs": len(removed),
                },
            )
            logger.info(
                "absent sweep %s:%s %s urls=%d docs=%d",
                ecosystem,
                library,
                version,
                len(removed_urls),
                len(removed),
            )
        except Exception as exc:  # noqa: BLE001 — deletion-log failure does not overturn the source-of-truth deletion
            logger.debug("absent log failed: %s", exc)
    return removed


async def sync_vectors(
    conn,
    settings,
    vec,
    embed,
    store: DocumentStore,
    counts: dict[str, int],
    removed: list[int],
    inserted: list[int],
    *,
    version_id: int | None = None,
    repair: bool = True,
    verify: bool = True,
) -> None:
    from libref.services.ops.maintenance import sync_vectors_for_docs

    await sync_vectors_for_docs(
        conn,
        settings,
        vec,
        embed,
        store,
        counts,
        removed,
        inserted,
        version_id=version_id,
        repair=repair,
        verify=verify,
    )
