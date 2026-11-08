"""MCP 出口：FastMCP 实例与工具注册（只调 services/）。

工具体只做参数解析 + services 调用 + 结果格式化（module-layout §2 出口零业务逻辑）。
stdio 传输由 CLI `serve-mcp` 启动。
"""

from __future__ import annotations

import sqlite3
from typing import Any

try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    """mcp>=2 重命名 FastMCP → MCPServer；别名保持注册/运行调用不变。"""
except ImportError:
    from mcp.server.fastmcp import FastMCP

from libref.config import LibrefSettings
from libref.crawler.registry import RegistryError
from libref.services.resolver import resolve_package as _resolve_package
from libref.services.search import SearchService


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None
) -> FastMCP:
    """构建 FastMCP 实例并注册文档检索工具；连接由调用方管理生命周期。"""
    mcp: FastMCP = FastMCP("libref")
    search = SearchService(conn)
    resolve_settings = settings or LibrefSettings()

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
    async def resolve_package(
        ecosystem: str | None,
        name: str,
        version: str | None = None,
    ) -> dict[str, Any]:
        """按生态加名称解析文档源，只读，不触发抓取。"""
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=resolve_settings.request_timeout,
                follow_redirects=True,
                headers={"User-Agent": resolve_settings.user_agent},
                http2=True,
            ) as client:
                resolution = await _resolve_package(
                    conn, client, resolve_settings, ecosystem, name, version,
                )
        except ValueError as exc:
            return {"error": "invalid", "message": str(exc)}
        except RegistryError as exc:
            return {"error": exc.kind, "message": str(exc), "hint": exc.hint}
        return resolution.model_dump()

    return mcp
