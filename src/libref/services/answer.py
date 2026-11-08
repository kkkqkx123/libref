"""问答编排：混合检索取候选，问答只依据材料作答并回传引用。"""

from __future__ import annotations

import asyncio
from typing import Any

from libref.llm.chat import ChatFn, build_chat_fn
from libref.llm.errors import LlmConfigError

_SYSTEM_PROMPT = "只依据所给材料回答问题，材料不足时直说不知道，引用时使用材料编号。"


def _material_block(index: int, item: dict[str, Any]) -> str:
    title = item.get("title") or item.get("url")
    return f"[{index}] {title}\n{item.get('url')}\n{item.get('snippet')}"


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
) -> dict[str, Any]:
    """检索加生成问答，无候选时不调用问答，问答未启用时直接拒绝。"""
    from libref.store.retriever import HybridRetriever

    if not query.strip():
        raise ValueError("query is empty")
    if limit <= 0:
        raise ValueError("limit must be positive")
    answer_chat = chat if chat is not None else build_chat_fn(settings)
    if answer_chat is None:
        raise LlmConfigError("chat is not enabled: enable chat and set an api key")
    retriever = HybridRetriever(
        conn, settings, vec_store=vec_store, embed=embed, rerank=rerank
    )
    results, degraded = await retriever.search_async(
        query, library=library, version=version, ecosystem=ecosystem, limit=limit
    )
    if not results:
        return {"answer": "", "citations": [], "degraded": degraded, "model": ""}
    materials = "\n\n".join(
        _material_block(i + 1, item) for i, item in enumerate(results)
    )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"{query}\n\n材料：\n{materials}"},
    ]
    answer = await answer_chat(messages)
    citations = [
        {"url": item["url"], "title": item.get("title")} for item in results
    ]
    return {
        "answer": answer,
        "citations": citations,
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
) -> dict[str, Any]:
    """问答的同步入口，供命令行直接调用。"""
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
        )
    )
