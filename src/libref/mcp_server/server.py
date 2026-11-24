"""MCPServer instance and tool registration (services only)."""

from __future__ import annotations

import sqlite3

from mcp.server.mcpserver import MCPServer

from libref.config import LibrefSettings, load_settings
from libref.mcp_server.context import ALL_MCP_TOOLS, McpContext, _effective_tools

__all__ = ["ALL_MCP_TOOLS", "build_mcp_server"]
from libref.mcp_server.contract import install as install_contract
from libref.observe import setup_logging
from libref.services.code.mining_read import MiningReadService
from libref.services.docs.pages import PageService
from libref.services.factory import (
    build_discovery_service,
    build_gateway,
    build_search_service,
    build_vector,
)
from libref.services.ops.review import ReviewService
from libref.services.registry.libraries import LibraryService


def build_mcp_server(
    conn: sqlite3.Connection, settings: LibrefSettings | None = None, context=None
) -> MCPServer:
    """Build the MCPServer instance and register only the visible tools."""
    resolve_settings = settings or load_settings()
    enabled = _effective_tools(resolve_settings)
    setup_logging(resolve_settings)
    mcp = MCPServer("libref")
    gateway = build_gateway(resolve_settings)
    vec_store, embed_fn, gateway = build_vector(resolve_settings, gateway)
    rerank_fn = gateway.rerank_fn()
    chat_fn = gateway.chat_fn()
    search_svc, _gateway = build_search_service(
        conn,
        resolve_settings,
        gateway=gateway,
        vec_store=vec_store,
        embed=embed_fn,
        rerank=rerank_fn,
    )
    discovery_svc = build_discovery_service(conn, resolve_settings)
    pages = PageService(conn)
    catalog_svc = LibraryService(conn)
    mining = MiningReadService(conn, resolve_settings)
    review_svc = ReviewService(conn)

    ctx = McpContext(
        conn,
        resolve_settings,
        context,
        search_svc,
        discovery_svc,
        pages,
        catalog_svc,
        mining,
        review_svc,
        gateway,
        vec_store,
        embed_fn,
        rerank_fn,
        chat_fn,
    )
    from libref.mcp_server.tools import discovery as discovery_tools
    from libref.mcp_server.tools import index_requests as index_request_tools
    from libref.mcp_server.tools import mining as mining_tools
    from libref.mcp_server.tools import ops as ops_tools
    from libref.mcp_server.tools import retrieval as retrieval_tools

    visible: dict[str, object] = {}
    for group in (
        retrieval_tools,
        discovery_tools,
        mining_tools,
        ops_tools,
        index_request_tools,
    ):
        visible.update(group.register(mcp, ctx, ctx.wrap))
    for name, func in visible.items():
        if name in enabled:
            mcp.tool()(ctx.wrap(name, func))

    install_contract(mcp)
    return mcp
