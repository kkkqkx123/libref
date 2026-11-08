"""命令行出口，只做参数解析加服务调用加结果打印。"""

from __future__ import annotations

import argparse
import sys

from libref.config import LibrefSettings
from libref.crawler.registry import RegistryError
from libref.db import apply_migrations, open_db
from libref.llm import LlmGateway
from libref.services.answer import answer_query
from libref.services.ingest import (
    INDEX_REPO_JOB_KIND,
    MINE_PATTERNS_JOB_KIND,
    IngestService,
    build_repo_runner,
    index_repo,
    mine_patterns,
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
    print(
        f"db ready at {args.db_path if hasattr(args, 'db_path') else 'data/libref.db'} "
        f"({applied} migrations applied)"
    )
    return 0


def applied_count(conn) -> int:
    from libref.db.migrations import MIGRATIONS, applied_versions

    return len(applied_versions(conn) & {m.version for m in MIGRATIONS})


def _build_vector(settings: LibrefSettings, gateway: LlmGateway | None = None):
    """按配置构造向量存储与编码函数，无密钥时返回空，走纯全文路。"""
    gw = gateway if gateway is not None else LlmGateway(settings)
    embed = gw.embed_fn()
    if embed is None:
        return None, None
    vec = QdrantVecStore(
        settings.qdrant_url,
        collection_for(settings.qdrant_collection, settings.db_path),
        timeout=settings.qdrant_timeout,
    )
    return vec, embed


def _scope_from_args(args: argparse.Namespace) -> dict:
    return {
        "allow_cross_site": bool(getattr(args, "allow_cross_site", False)),
        "allow_subdomains": bool(getattr(args, "allow_subdomains", False)),
        "path_prefixes": list(getattr(args, "path_prefix", None) or []),
        "path_suffixes": list(getattr(args, "path_suffix", None) or []),
        "include_patterns": list(getattr(args, "include_pattern", None) or []),
        "exclude_patterns": list(getattr(args, "exclude_pattern", None) or []),
        "max_depth": getattr(args, "max_depth", None),
    }


def _resolve_for_cli(settings, conn, ecosystem, library, requested_version):
    """命令行解析形态：生态加名称解析出地址与版本，失败打印分类错误。"""
    import asyncio

    import httpx

    from libref.services.resolver import resolve_package

    async def _run():
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": getattr(settings, "user_agent", "libref-crawler/0.1")
            },
            http2=True,
        ) as client:
            return await resolve_package(
                conn,
                client,
                settings,
                ecosystem,
                library,
                requested_version,
            )

    return asyncio.run(_run())


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = LibrefSettings()
    if getattr(args, "offline", False):
        settings.registry_offline = True
    conn = _connect(settings)
    vec_store, embed = _build_vector(settings)
    scope = _scope_from_args(args)
    try:
        resolve_mode = args.ecosystem is not None or args.start_url is None
        if resolve_mode:
            if not args.library:
                print("resolve mode requires library (package name)", file=sys.stderr)
                return 2
            if args.start_url is not None:
                print(
                    "--ecosystem and start_url are mutually exclusive", file=sys.stderr
                )
                return 2
            try:
                resolution = _resolve_for_cli(
                    settings,
                    conn,
                    args.ecosystem,
                    args.library,
                    args.version,
                )
            except ValueError as exc:
                print(f"invalid: {exc}", file=sys.stderr)
                return 2
            except RegistryError as exc:
                hint = f" hint={exc.hint}" if exc.hint else ""
                print(f"resolve failed [{exc.kind}]: {exc}{hint}", file=sys.stderr)
                return 1
            assert resolution.docs_url is not None
            library, version, start_url = (
                resolution.name,
                resolution.version,
                resolution.docs_url,
            )
            ecosystem = resolution.ecosystem
            resolved_from: str | None = resolution.source
            registry_confirmed = resolution.registry_confirmed
            homepage, repo_url = resolution.homepage, resolution.repo_url
            print(
                f"resolved {ecosystem}:{library} {version} -> {start_url} [{resolved_from}]"
                + (" (version unconfirmed)" if not registry_confirmed else "")
            )
        else:
            if not (args.library and args.version and args.start_url):
                print(
                    "explicit mode requires library version start_url", file=sys.stderr
                )
                return 2
            library, version, start_url = args.library, args.version, args.start_url
            ecosystem, resolved_from = "custom", "explicit"
            registry_confirmed, homepage, repo_url = True, None, None
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = start_scrape_job(
                mgr,
                library,
                version,
                start_url,
                max_pages=args.max_pages,
                prefilter=args.prefilter,
                scope=scope,
                ecosystem=ecosystem,
                resolved_from=resolved_from,
                registry_confirmed=registry_confirmed,
            )
            job = run_scrape_job_sync(
                mgr, settings, conn, job_id, vec_store=vec_store, embed=embed
            )
            print(
                f"job {job.id} {job.status}"
                + (f" error={job.error}" if job.error else "")
            )
            return 0 if job.status == "done" else 1
        service = IngestService(settings, conn, vec_store=vec_store, embed=embed)
        counts = service.scrape_docs(
            library,
            version,
            start_url,
            max_pages=args.max_pages,
            prefilter=args.prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            homepage=homepage,
            repo_url=repo_url,
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_index_repo(args: argparse.Namespace) -> int:
    import asyncio

    settings = LibrefSettings()
    conn = _connect(settings)
    repos_dir = settings.db_path.parent / "repos"
    try:
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                INDEX_REPO_JOB_KIND,
                {
                    "library": args.library,
                    "version": args.version,
                    "source": args.source,
                    "update": bool(args.update),
                },
            )

            async def _drive():
                mgr.start_worker(build_repo_runner(settings, conn, repos_dir))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            print(
                f"job {job.id} {job.status}"
                + (f" error={job.error}" if job.error else "")
            )
            return 0 if job.status == "done" else 1
        counts = index_repo(
            conn,
            args.library,
            args.version,
            args.source,
            update=bool(args.update),
            repos_dir=repos_dir,
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    import asyncio
    import json

    settings = LibrefSettings()
    conn = _connect(settings)
    try:
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                MINE_PATTERNS_JOB_KIND,
                {
                    "library": args.library,
                    "version": args.version,
                    "minsup": args.minsup,
                    "topn": args.topn,
                    "maxlen": args.maxlen,
                    "minlen": args.minlen,
                    "include_ambiguous": bool(args.include_ambiguous),
                    "cluster_threshold": args.cluster_threshold,
                    "cluster_min_size": args.cluster_min_size,
                },
            )

            async def _drive():
                mgr.start_worker(build_repo_runner(settings, conn))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            print(
                f"job {job.id} {job.status}"
                + (f" error={job.error}" if job.error else "")
            )
            return 0 if job.status == "done" else 1
        results = mine_patterns(
            conn,
            args.library,
            args.version,
            minsup=args.minsup,
            topn=args.topn,
            maxlen=args.maxlen,
            minlen=args.minlen,
            include_ambiguous=bool(args.include_ambiguous),
            cluster_threshold=args.cluster_threshold,
            cluster_min_size=args.cluster_min_size,
        )
    finally:
        conn.close()
    for item in results:
        print(f"{item['support']} {json.dumps(item['pattern'], ensure_ascii=False)}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = _connect(LibrefSettings())
    try:
        results = SearchService(conn).search_docs(
            args.query,
            library=args.library,
            version=args.version,
            limit=args.limit,
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


def cmd_answer(args: argparse.Namespace) -> int:
    settings = LibrefSettings()
    conn = _connect(settings)
    try:
        gateway = LlmGateway(settings)
        vec_store, embed = _build_vector(settings, gateway)
        out = answer_query(
            conn,
            settings,
            args.query,
            library=args.library,
            version=args.version,
            limit=args.limit,
            vec_store=vec_store,
            embed=embed,
            chat=gateway.chat_fn(),
        )
    finally:
        conn.close()
    if not out["answer"]:
        print("(no evidence)")
        return 0
    print(out["answer"])
    for c in out["citations"]:
        print(f"- {c['title'] or '(untitled)'} — {c['url']}")
    if out["degraded"]:
        print("(degraded: partial retrieval)", file=sys.stderr)
    return 0


def cmd_read_page(args: argparse.Namespace) -> int:
    conn = _connect(LibrefSettings())
    try:
        page = SearchService(conn).read_page(
            args.url,
            library=args.library,
            version=args.version,
        )
    finally:
        conn.close()
    if page is None:
        print(f"(page not found: {args.url})", file=sys.stderr)
        return 1
    print(page["content"])
    return 0


def cmd_feature_tree(args: argparse.Namespace) -> int:
    import json

    conn = _connect(LibrefSettings())
    try:
        rows = SearchService(conn).feature_tree(
            args.library,
            args.version,
            prefix=args.prefix,
            limit=args.limit,
        )
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_clusters(args: argparse.Namespace) -> int:
    import json

    conn = _connect(LibrefSettings())
    try:
        containing = args.containing.split(",") if args.containing else None
        rows = SearchService(conn).api_clusters(
            args.library,
            args.version,
            containing=containing,
            limit=args.limit,
        )
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_dump_chunks(args: argparse.Namespace) -> int:
    from pathlib import Path

    from libref.chunking import default_budgets, split_markdown_dual

    budgets = default_budgets()
    if args.markdown is not None:
        text = Path(args.markdown).read_text(encoding="utf-8")
        url = args.url or "preview://local"
        dual = split_markdown_dual(text, url, budgets=budgets)
        for stream in (dual.embedding, dual.bm25):
            for chunk in stream:
                print(
                    f"[{chunk.chunk_id}] {chunk.path or '(root)'} | "
                    f"{chunk.title} | kind={chunk.span_kind} "
                    f"merged={chunk.merged_from} "
                    f"overlap_from={chunk.overlap_from}"
                )
                print(f"    {chunk.content[:160].replace(chr(10), ' ')}")
        print(
            f"embedding={len(dual.embedding)} bm25={len(dual.bm25)} "
            f"empty_dropped={dual.empty_dropped} deduped={dual.deduped}"
        )
        return 0
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine
    from libref.astminer import grouping as ast_grouping
    from libref.astminer import source_chunk as ast_source_chunk

    data = Path(args.source).read_bytes()
    suffix = Path(args.source).suffix
    cfg = ast_config.config_for_suffix(suffix)
    if cfg is None:
        print(f"unsupported suffix: {suffix}", file=sys.stderr)
        return 2
    decls, _aliases, _raw = ast_engine.extract_file(args.source, cfg, data)
    groups = ast_grouping.build_groups(decls, data, args.source)
    by_qual = {d.qualified_name: d for d in decls}
    split = ast_source_chunk.build_source_chunks(
        groups, by_qual, data, args.source, budgets=budgets, repo="preview"
    )
    for group in groups:
        print(f"group {group.group_id} kind={group.kind} members={group.member_quals}")
    for chunk in split.chunks:
        print(
            f"[{chunk.chunk_id}] {chunk.qual} kind={chunk.span_kind} "
            f"bytes={chunk.start_byte}-{chunk.end_byte} merged={chunk.merged_quals} "
            f"overlap_from={chunk.overlap_from}"
        )
        print(f"    {chunk.content[:160].replace(chr(10), ' ')}")
    print(f"chunks={len(split.chunks)} empty_dropped={split.empty_dropped}")
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

    sub.add_parser("init-db", help="初始化数据库（应用迁移）").set_defaults(
        func=cmd_init_db
    )

    p_ingest = sub.add_parser("ingest", help="抓取文档站并入库")
    p_ingest.add_argument("library", nargs="?", help="显式形态为库名，解析形态为包名")
    p_ingest.add_argument(
        "version", nargs="?", default=None, help="显式形态必填，解析形态缺省取稳定版"
    )
    p_ingest.add_argument(
        "start_url", nargs="?", default=None, help="显式形态必填，与 --ecosystem 互斥"
    )
    p_ingest.add_argument("--ecosystem", default=None, help="按名称解析：pypi|npm")
    p_ingest.add_argument("--offline", action="store_true", help="只读缓存，不请求网络")
    p_ingest.add_argument("--max-pages", type=int, default=None)
    p_ingest.add_argument("--prefilter", action="store_true", help="HEAD 存活预筛")
    p_ingest.add_argument(
        "--async",
        dest="run_async",
        action="store_true",
        help="经 jobs 队列执行（提交→同进程 worker 消费→回写 jobs 表）",
    )
    p_ingest.add_argument(
        "--allow-cross-site", action="store_true", help="允许跨站抓取"
    )
    p_ingest.add_argument(
        "--allow-subdomains", action="store_true", help="允许同域子域"
    )
    p_ingest.add_argument(
        "--path-prefix", action="append", default=None, help="仅接受此前缀路径，可重复"
    )
    p_ingest.add_argument(
        "--path-suffix", action="append", default=None, help="仅接受此后缀路径，可重复"
    )
    p_ingest.add_argument(
        "--include-pattern", action="append", default=None, help="正则白名单，可重复"
    )
    p_ingest.add_argument(
        "--exclude-pattern", action="append", default=None, help="正则黑名单，可重复"
    )
    p_ingest.add_argument(
        "--max-depth", type=int, default=None, help="相对起始目录的最大深度"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_search = sub.add_parser("search", help="FTS 全文检索")
    p_search.add_argument("query")
    p_search.add_argument("--library", default=None)
    p_search.add_argument("--version", default=None)
    p_search.add_argument("--limit", type=int, default=8)
    p_search.set_defaults(func=cmd_search)

    p_answer = sub.add_parser("answer", help="混合检索加问答生成")
    p_answer.add_argument("query")
    p_answer.add_argument("--library", default=None)
    p_answer.add_argument("--version", default=None)
    p_answer.add_argument("--limit", type=int, default=5)
    p_answer.set_defaults(func=cmd_answer)

    p_read = sub.add_parser("read-page", help="按 URL 读取整页文档")
    p_read.add_argument("url")
    p_read.add_argument("--library", default=None)
    p_read.add_argument("--version", default=None)
    p_read.set_defaults(func=cmd_read_page)

    sub.add_parser("serve-mcp", help="以 stdio 传输启动 MCP 服务").set_defaults(
        func=cmd_serve_mcp
    )

    p_repo = sub.add_parser("index-repo", help="索引源码仓库并落调用实例")
    p_repo.add_argument("library")
    p_repo.add_argument("version")
    p_repo.add_argument("source", help="远端地址或本地路径")
    p_repo.add_argument("--update", action="store_true", help="已存在仓库时拉取更新")
    p_repo.add_argument(
        "--async", dest="run_async", action="store_true", help="经 jobs 队列执行"
    )
    p_repo.set_defaults(func=cmd_index_repo)

    p_mine = sub.add_parser("mine", help="挖掘闭合调用序列模式")
    p_mine.add_argument("library")
    p_mine.add_argument("version")
    p_mine.add_argument("--minsup", type=int, default=2)
    p_mine.add_argument(
        "--topn",
        type=int,
        default=None,
        help="显式剪枝：只保留调用次数前 N 的完整限定名",
    )
    p_mine.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="将待定调用纳入序列（默认只用显式与推导）",
    )
    p_mine.add_argument("--maxlen", type=int, default=10)
    p_mine.add_argument("--minlen", type=int, default=2)
    p_mine.add_argument("--cluster-threshold", type=float, default=0.15)
    p_mine.add_argument("--cluster-min-size", type=int, default=2)
    p_mine.add_argument(
        "--async", dest="run_async", action="store_true", help="经 jobs 队列执行"
    )
    p_mine.set_defaults(func=cmd_mine)

    p_tree = sub.add_parser("feature-tree", help="按前缀浏览分层特征")
    p_tree.add_argument("library")
    p_tree.add_argument("version")
    p_tree.add_argument("--prefix", default=None)
    p_tree.add_argument("--limit", type=int, default=100)
    p_tree.set_defaults(func=cmd_feature_tree)

    p_clusters = sub.add_parser("clusters", help="浏览接口分组")
    p_clusters.add_argument("library")
    p_clusters.add_argument("version")
    p_clusters.add_argument("--containing", default=None, help="逗号分隔的必含接口")
    p_clusters.add_argument("--limit", type=int, default=50)
    p_clusters.set_defaults(func=cmd_clusters)

    p_dump = sub.add_parser("dump-chunks", help="预览分块：文件到分块的行号字节标题")
    group = p_dump.add_mutually_exclusive_group(required=True)
    group.add_argument("--markdown", default=None, help="Markdown 文档路径")
    group.add_argument("--source", default=None, help="源码文件路径")
    p_dump.add_argument("--url", default=None, help="文档块地址标注")
    p_dump.set_defaults(func=cmd_dump_chunks)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
