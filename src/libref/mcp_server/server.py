"""MCP 出口：MCPServer 实例与工具注册（只调 services/）。

工具体只做参数解析 + services 调用 + 结果格式化（module-layout §2 出口零业务逻辑）。
传输（stdio / Streamable HTTP）由 CLI `serve-mcp` 选择启动；SSE 已被官方废弃，不提供。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server.mcpserver import MCPServer

from libref.config import LibrefSettings, load_settings
from libref.crawler.registry import RegistryError
from libref.llm import LlmGateway
from libref.observe import setup_logging
from libref.services.resolver import resolve_package as _resolve_package
from libref.services.review import ReviewService
from libref.services.search import SearchService


def _wired_search(conn, settings):
    """按配置装配检索服务，无密钥时退化为纯全文。"""
    gateway = LlmGateway(settings)
    embed = gateway.embed_fn()
    rerank_fn = None
    try:
        rerank_fn = gateway.rerank_fn()
    except Exception:  # noqa: BLE001 —— 重排不可用即关闭，不阻塞全文
        rerank_fn = None
    vec_store = None
    if embed is not None:
        from libref.store.vec_store import QdrantVecStore, collection_for

        vec_store = QdrantVecStore(
            settings.qdrant_url,
            collection_for(settings.qdrant_collection, settings.db_path),
            timeout=settings.qdrant_timeout,
        )
    return SearchService(conn, settings, vec_store=vec_store, embed=embed, rerank=rerank_fn), gateway


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None
) -> MCPServer:
    """构建 MCPServer 实例并注册文档检索工具；连接由调用方管理生命周期。"""
    resolve_settings = settings or load_settings()
    setup_logging(resolve_settings)
    mcp = MCPServer("libref")
    search, gateway = _wired_search(conn, resolve_settings)
    review = ReviewService(conn)

    @mcp.tool()
    def search_docs(
        query: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """全文检索文档库，返回按相关度排序的片段（url/title/path/snippet）。"""
        return search.search_docs(query, library=library, version=version, limit=limit)

    @mcp.tool()
    def read_page(url: str, library: str | None = None, version: str | None = None) -> dict[str, Any] | None:
        """按 URL 读取整页文档内容（分块按序重组）。"""
        return search.read_page(url, library=library, version=version)

    @mcp.tool()
    def read_window(url: str, sort_order: int, radius: int = 1) -> dict[str, Any] | None:
        """按序号取前后窗口，用于命中后扩展上下文。"""
        return search.read_window(url, sort_order, radius)

    @mcp.tool()
    def read_section(url: str, source_group_id: str) -> dict[str, Any] | None:
        """按源分组取整节，用于由子块回溯父节。"""
        return search.read_section(url, source_group_id)

    @mcp.tool()
    def list_libraries() -> list[dict[str, Any]]:
        """列出全部文档库及其版本数。"""
        return search.list_libraries()

    @mcp.tool()
    def list_versions(
        library: str, ecosystem: str | None = None
    ) -> list[dict[str, Any]]:
        """列出指定库的全部版本及页面数。"""
        return search.list_versions(library, ecosystem)

    @mcp.tool()
    def api_usage(
        library: str | None = None,
        version: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """统计版本内各接口的调用次数与覆盖作用域数（频次查询）。"""
        return search.api_usage_counts(library=library, version=version, limit=limit)

    @mcp.tool()
    def related_apis(
        api: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """查询与指定接口同作用域共现的接口（共现查询）。"""
        return search.related_apis(api, library=library, version=version, limit=limit)

    @mcp.tool()
    def pattern_evidence(
        pattern: list[str],
        library: str | None = None,
        version: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """按调用序列模式取来源证据（文件、外围调用者、行范围、实参、置信）。"""
        return search.pattern_evidence(pattern, library=library, version=version, limit=limit)

    @mcp.tool()
    def feature_tree(
        library: str | None = None,
        version: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按前缀浏览分层特征节点。"""
        return search.feature_tree(library=library, version=version, prefix=prefix, limit=limit)

    @mcp.tool()
    def api_clusters(
        library: str | None = None,
        version: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """浏览接口分组。"""
        return search.api_clusters(library=library, version=version, limit=limit)

    @mcp.tool()
    async def search_hybrid(
        query: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 8,
        window_radius: int | None = None,
        expand_window: bool = False,
    ) -> dict[str, Any]:
        """混合检索文档库，与命令行 search --hybrid 同序，附降级标记。"""
        results, degraded = await search.search_hybrid_async(
            query, library=library, version=version, limit=limit,
            expand_window=expand_window or None, window_radius=window_radius,
        )
        return {"results": results, "degraded": degraded}

    @mcp.tool()
    async def answer(
        query: str,
        library: str | None = None,
        version: str | None = None,
        limit: int = 5,
        window_radius: int | None = None,
        expand_window: bool = False,
        material_limit: int | None = None,
    ) -> dict[str, Any]:
        """混合检索加问答生成，未启用时返回明确原因。"""
        from libref.services.answer import answer_query_async

        try:
            return await answer_query_async(
                conn, resolve_settings, query,
                library=library, version=version, limit=limit,
                expand_window=expand_window or None, window_radius=window_radius,
                material_limit=material_limit,
            )
        except ValueError as exc:
            return {"error": "unavailable", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 问答失败透出原因，不抛内部栈
            return {"error": "failed", "message": str(exc)}

    @mcp.tool()
    def review_pending(limit: int = 50) -> list[dict[str, Any]]:
        """列出待审核调用实例，与命令行 review list 同序。"""
        return review.list_pending(limit=limit)

    @mcp.tool()
    def review_approve(entity_id: int) -> dict[str, Any]:
        """待定转已核准，非法转移返回原因。"""
        try:
            review.approve(entity_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def review_conflict(entity_id: int) -> dict[str, Any]:
        """待定标冲突，非法转移返回原因。"""
        try:
            review.mark_conflict(entity_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def review_reopen(entity_id: int) -> dict[str, Any]:
        """冲突重开为待定，非冲突返回原因。"""
        try:
            review.reopen(entity_id)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True}

    @mcp.tool()
    def job_show(job_id: int | None = None, limit: int = 20) -> dict[str, Any]:
        """查看任务结论，缺标识时列出最近任务。"""
        from libref.services.jobs import JobManager

        mgr = JobManager(conn)
        if job_id is None:
            return {"jobs": [
                {"id": j.id, "kind": j.kind, "status": j.status,
                 "finished_at": j.finished_at, "error": j.error}
                for j in mgr.list_recent(limit=limit)
            ]}
        job = mgr.get(job_id)
        if job is None:
            return {"error": "not_found", "message": f"unknown job: {job_id}"}
        return {"id": job.id, "kind": job.kind, "status": job.status,
                "error": job.error, "result": job.result,
                "finished_at": job.finished_at}

    @mcp.tool()
    async def resolve_package(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        """按生态加名称解析文档源，只读，不触发抓取。"""
        from libref.crawler.http import build_async_client

        try:
            async with build_async_client(resolve_settings) as client:
                resolution = await _resolve_package(
                    conn, client, resolve_settings, ecosystem, name, version,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    @mcp.tool()
    async def resolve_repo(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        """按生态加名称解析仓库地址，只读，不触发索引。"""
        from libref.crawler.http import build_async_client
        from libref.services.resolver import resolve_repo as _resolve_repo

        try:
            async with build_async_client(resolve_settings) as client:
                resolution = await _resolve_repo(
                    conn, client, resolve_settings, ecosystem, name, version,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    return mcp
