"""命令行出口，只做参数解析加服务调用加结果打印。"""

from __future__ import annotations

import argparse
import sys

from libref.config import LibrefSettings
from libref.db import apply_migrations, open_db
from libref.services.ingest import (
    IngestService,
    build_embed_fn,
    run_scrape_job_sync,
    start_scrape_job,
)
from libref.services.jobs import JobManager
from libref.services.search import SearchService
from libref.store.vec_store import QdrantVecStore, collection_for


def _connect(settings: LibrefSettings, *, check_same_thread: bool = True):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(settings.db_path, check_same_thread=check_same_thread)
    apply_migrations(conn)
    return conn


def cmd_init_db(args: argparse.Namespace) -> int:
    conn = _connect(LibrefSettings())
    applied = applied_count(conn)
    conn.close()
    print(f"db ready at {args.db_path if hasattr(args, 'db_path') else 'data/libref.db'} "
          f"({applied} migrations applied)")
    return 0


def applied_count(conn) -> int:
    from libref.db.migrations import MIGRATIONS, applied_versions
    return len(applied_versions(conn) & {m.version for m in MIGRATIONS})


def _build_vector(settings: LibrefSettings):
    """按配置构造向量存储与编码函数，无密钥时返回空，走纯全文路。"""
    embed = build_embed_fn(settings)
    if embed is None:
        return None, None
    vec = QdrantVecStore(
        settings.qdrant_url,
        collection_for(settings.qdrant_collection, settings.db_path),
        timeout=settings.qdrant_timeout,
    )
    return vec, embed


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = LibrefSettings()
    conn = _connect(settings)
    vec_store, embed = _build_vector(settings)
    try:
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = start_scrape_job(
                mgr, args.library, args.version, args.start_url,
                max_pages=args.max_pages, prefilter=args.prefilter,
            )
            job = run_scrape_job_sync(mgr, settings, conn, job_id,
                                        vec_store=vec_store, embed=embed)
            print(f"job {job.id} {job.status}" + (f" error={job.error}" if job.error else ""))
            return 0 if job.status == "done" else 1
        service = IngestService(settings, conn, vec_store=vec_store, embed=embed)
        counts = service.scrape_docs(
            args.library, args.version, args.start_url,
            max_pages=args.max_pages, prefilter=args.prefilter,
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = _connect(LibrefSettings())
    try:
        results = SearchService(conn).search_docs(
            args.query, library=args.library, version=args.version, limit=args.limit,
        )
    finally:
        conn.close()
    if not results:
        print("(no results)")
        return 0
    for r in results:
        print(f"[{r['score']:.3f}] {r['title'] or '(untitled)'} — {r['url']}")
        print(f"    {r['snippet']}")
    return 0


def cmd_read_page(args: argparse.Namespace) -> int:
    conn = _connect(LibrefSettings())
    try:
        page = SearchService(conn).read_page(
            args.url, library=args.library, version=args.version,
        )
    finally:
        conn.close()
    if page is None:
        print(f"(page not found: {args.url})", file=sys.stderr)
        return 1
    print(page["content"])
    return 0


def cmd_serve_mcp(_args: argparse.Namespace) -> int:
    from libref.mcp_server import build_mcp_server

    # MCPServer 在 worker 线程执行工具，连接需跨线程复用。
    conn = _connect(LibrefSettings(), check_same_thread=False)
    mcp = build_mcp_server(conn)
    mcp.run("stdio")  # 阻塞直到 stdio 关闭
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libref", description="libref 文档知识库")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库（应用迁移）").set_defaults(func=cmd_init_db)

    p_ingest = sub.add_parser("ingest", help="抓取文档站并入库")
    p_ingest.add_argument("library")
    p_ingest.add_argument("version")
    p_ingest.add_argument("start_url")
    p_ingest.add_argument("--max-pages", type=int, default=None)
    p_ingest.add_argument("--prefilter", action="store_true", help="HEAD 存活预筛")
    p_ingest.add_argument("--async", dest="run_async", action="store_true",
                          help="经 jobs 队列执行（提交→同进程 worker 消费→回写 jobs 表）")
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser("search", help="FTS 全文检索")
    p_search.add_argument("query")
    p_search.add_argument("--library", default=None)
    p_search.add_argument("--version", default=None)
    p_search.add_argument("--limit", type=int, default=8)
    p_search.set_defaults(func=cmd_search)

    p_read = sub.add_parser("read-page", help="按 URL 读取整页文档")
    p_read.add_argument("url")
    p_read.add_argument("--library", default=None)
    p_read.add_argument("--version", default=None)
    p_read.set_defaults(func=cmd_read_page)

    sub.add_parser("serve-mcp", help="以 stdio 传输启动 MCP 服务").set_defaults(func=cmd_serve_mcp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
