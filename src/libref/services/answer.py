"""Grounded question answering: hybrid retrieval plus cited generation."""

from __future__ import annotations

import asyncio
from typing import Any

from libref.llm.chat import ChatFn, build_chat_fn
from libref.llm.errors import LlmConfigError
from libref.services.answer_materials import (
    build_citations,
    build_messages,
)


async def answer_query_async(
    conn,
    settings,
    query: str,
    *,
    library: str | None = None,
    version: str | None = None,
    ecosystem: str | None = None,
    limit: int = 5,
    vec_store=None,
    embed=None,
    rerank=None,
    chat: ChatFn | None = None,
    expand_window: bool | None = None,
    window_radius: int | None = None,
    material_limit: int | None = None,
) -> dict[str, Any]:
    """Retrieve candidates then generate an answer grounded in those materials."""
    from libref.services.search import SearchService
    from libref.store.document_store import DocumentStore

    if not query.strip():
        raise ValueError("query is empty")
    if limit <= 0:
        raise ValueError("limit must be positive")
    answer_chat = chat if chat is not None else build_chat_fn(settings)
    if answer_chat is None:
        raise LlmConfigError("chat is not enabled: enable chat and set an api key")
    search = SearchService(
        conn, settings, vec_store=vec_store, embed=embed, rerank=rerank
    )
    results, degraded = await search.search_hybrid_async(
        query,
        library=library,
        version=version,
        ecosystem=ecosystem,
        limit=limit,
        expand_window=expand_window,
        window_radius=window_radius,
    )
    if not results:
        return {"answer": "", "citations": [], "degraded": degraded, "model": ""}
    try:
        rows = DocumentStore(conn).get_documents([int(r["id"]) for r in results])
        full_by_id = {int(r["id"]): str(r.get("content") or "") for r in rows}
    except Exception:  # noqa: BLE001 — missing full content degrades gracefully to empty materials
        full_by_id = {}
    cap = settings.material_limit if material_limit is None else material_limit
    labels: list[dict[str, Any]] = []
    try:
        from libref.services.mining_read import MiningReadService

        candidates = MiningReadService(conn).list_labels(
            library=library, version=version, ecosystem=ecosystem, limit=20
        )
        entities = {str(e) for r in results for e in (r.get("entities") or []) if e}
        if entities and candidates:
            ranked = sorted(
                enumerate(candidates),
                key=lambda kv: (
                    -len(
                        (
                            set(kv[1].get("pattern") or [])
                            | set(kv[1].get("related_apis") or [])
                        )
                        & entities
                    ),
                    kv[0],
                ),
            )
            labels = [item for _, item in ranked[:3]]
        else:
            labels = candidates[:3]
    except Exception:  # noqa: BLE001 — labels are display-only enrichment
        labels = []
    messages = build_messages(query, results, full_by_id, cap, labels=labels)
    answer = await answer_chat(messages)
    return {
        "answer": answer,
        "citations": build_citations(results),
        "degraded": degraded,
        "model": settings.chat_model,
    }


def answer_query(
    conn,
    settings,
    query: str,
    *,
    library: str | None = None,
    version: str | None = None,
    ecosystem: str | None = None,
    limit: int = 5,
    vec_store=None,
    embed=None,
    rerank=None,
    chat: ChatFn | None = None,
    expand_window: bool | None = None,
    window_radius: int | None = None,
    material_limit: int | None = None,
) -> dict[str, Any]:
    """Synchronous entry for command-line use."""
    return asyncio.run(
        answer_query_async(
            conn,
            settings,
            query,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            vec_store=vec_store,
            embed=embed,
            rerank=rerank,
            chat=chat,
            expand_window=expand_window,
            window_radius=window_radius,
            material_limit=material_limit,
        )
    )
