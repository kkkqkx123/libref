"""Grounded question answering: preset retrieval plus cited generation."""

from __future__ import annotations

import asyncio
from typing import Any

from libref.llm.chat import ChatFn, build_chat_fn
from libref.llm.errors import LlmConfigError
from libref.services.docs.answer_materials import (
    apply_total_budget,
    build_citations,
    build_messages,
    filter_citations_by_answer,
)


def _run_coro(coro):
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        holder: dict[str, Any] = {}

        def _target() -> None:
            holder["result"] = asyncio.run(coro)

        import threading

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join()
        return holder["result"]
    return asyncio.run(coro)


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
    preset: str = "balanced",
) -> dict[str, Any]:
    """Retrieve candidates then generate an answer grounded in those materials."""
    from libref.services.docs.search import SearchService, normalize_preset
    from libref.store.document_store import DocumentStore

    if not query.strip():
        raise ValueError("query is empty")
    if limit <= 0:
        raise ValueError("limit must be positive")
    use_preset = normalize_preset(preset)
    answer_chat = chat if chat is not None else build_chat_fn(settings)
    if answer_chat is None:
        raise LlmConfigError("chat is not enabled: enable chat and set an api key")
    search = SearchService(
        conn, settings, vec_store=vec_store, embed=embed, rerank=rerank
    )
    results, status = await search.search_with_mode(
        query,
        use_preset,
        library=library,
        version=version,
        ecosystem=ecosystem,
        limit=limit,
    )
    degraded = bool(status.get("degraded", False))
    reasons = list(status.get("reasons", []) or [])
    if not results:
        return {
            "answer": "",
            "citations": [],
            "degraded": degraded,
            "reasons": reasons,
            "model": "",
            "confidence": "low",
            "cited_ids": [],
        }
    try:
        rows = DocumentStore(conn).get_documents([int(r["id"]) for r in results])
        full_by_id = {int(r["id"]): str(r.get("content") or "") for r in rows}
    except Exception:  # noqa: BLE001 — missing full content degrades gracefully to empty materials
        full_by_id = {}
    per_limit = int(settings.material_limit)
    try:
        total_limit = int(settings.answer_total_budget)
    except Exception:  # noqa: BLE001 -- fall back to per-material budgeting
        total_limit = per_limit * max(1, len(results))
    budgeted = apply_total_budget(results, full_by_id, per_limit, total_limit)
    labels: list[dict[str, Any]] = []
    try:
        entities = {str(e) for r in results for e in (r.get("entities") or []) if e}
        if entities and use_preset != "precise":
            from libref.services.code.mining_read import MiningReadService

            candidates = MiningReadService(conn).list_labels(
                library=library, version=version, ecosystem=ecosystem, limit=20
            )
            if candidates:
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
    except Exception:  # noqa: BLE001 — labels are display-only enrichment
        labels = []
    messages = build_messages(query, results, budgeted, per_limit, labels=labels)
    answer = await answer_chat(messages)
    kept_results, cited = filter_citations_by_answer(results, answer)
    return {
        "answer": answer,
        "citations": build_citations(kept_results),
        "degraded": degraded,
        "reasons": reasons,
        "model": settings.chat_model,
        "confidence": "high" if cited else "low",
        "cited_ids": [int(r["id"]) for r in kept_results],
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
    preset: str = "balanced",
) -> dict[str, Any]:
    """Synchronous entry for command-line use."""
    return _run_coro(
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
            preset=preset,
        )
    )
