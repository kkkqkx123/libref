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


def _empty_answer(degraded: bool, reasons: list[str]) -> dict[str, Any]:
    """No usable evidence: the caller must not pay for a generation call."""
    return {
        "answer": "",
        "citations": [],
        "degraded": degraded,
        "reasons": reasons,
        "model": "",
        "confidence": "low",
        "cited_ids": [],
    }


def _pattern_labels(conn, settings, results, library, version, ecosystem, preset):
    """Pick up to three mined labels overlapping the recalled entities; precise stays mining-free."""
    if preset == "precise":
        return []
    entities = {str(e) for r in results for e in (r.get("entities") or []) if e}
    if not entities:
        return []
    from libref.services.code.mining_read import MiningReadService

    candidates = MiningReadService(conn, settings).list_labels(
        library=library, version=version, ecosystem=ecosystem, limit=20
    )
    ranked = sorted(
        enumerate(candidates),
        key=lambda kv: (
            -len(
                (set(kv[1].get("pattern") or []) | set(kv[1].get("related_apis") or []))
                & entities
            ),
            kv[0],
        ),
    )
    return [item for _, item in ranked[:3]]


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
        expand_window=False,
    )
    degraded = bool(status.get("degraded", False))
    reasons = list(status.get("reasons", []) or [])
    if not results:
        return _empty_answer(degraded, reasons)
    rows = DocumentStore(conn).get_documents([int(r["id"]) for r in results])
    full_by_id = {int(r["id"]): str(r.get("content") or "") for r in rows}
    if any(int(r["id"]) not in full_by_id for r in results):
        degraded = True
        reasons.append("material_missing")
    per_limit = int(settings.material_limit)
    kept, bodies, omitted = apply_total_budget(
        results, full_by_id, per_limit, int(settings.answer_total_budget)
    )
    if not kept:
        return _empty_answer(degraded, reasons)
    labels = _pattern_labels(
        conn, settings, kept, library, version, ecosystem, use_preset
    )
    messages = build_messages(
        query, kept, bodies, per_limit, omitted=omitted, labels=labels
    )
    answer = await answer_chat(messages)
    cited_results, cited = filter_citations_by_answer(kept, answer)
    return {
        "answer": answer,
        "citations": build_citations(cited_results),
        "degraded": degraded,
        "reasons": list(dict.fromkeys(reasons)),
        "model": settings.chat_model,
        "confidence": "high" if cited else "low",
        "cited_ids": [int(r["id"]) for r in cited_results],
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
