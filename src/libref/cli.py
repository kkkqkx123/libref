"""命令行出口，只做参数解析加服务调用加结果打印。"""

from __future__ import annotations

import argparse
import sys

from libref.config import (
    ConfigError,
    LibrefSettings,
    load_settings,
    redacted_dict,
    resolve_config_file,
    settings_fingerprint,
)
from libref.crawler.registry import REGISTRY_MIRRORS, RegistryError
from libref.db import apply_migrations, open_db
from libref.observe import setup_logging
from libref.services.answer import answer_query
from libref.services.code_ingest import (
    INDEX_REPO_JOB_KIND,
    build_repo_runner,
    index_repo,
)
from libref.services.factory import build_search_service, build_vector
from libref.services.ingest import (
    IngestService,
    run_scrape_job_sync,
    start_refresh_job,
    start_scrape_job,
)
from libref.services.jobs import JobManager
from libref.services.libraries import LibraryService
from libref.services.maintenance import MaintenanceService
from libref.services.mining_read import MiningReadService
from libref.services.mining_refresh import MINE_PATTERNS_JOB_KIND, build_mine_runner, mine_job_params, mine_patterns
from libref.services.pages import PageService
from libref.services.review import ReviewService
from libref.services.search import SearchService


_config_parent = argparse.ArgumentParser(add_help=False)
_config_parent.add_argument(
    "--config",
    default=None,
    help="配置文件路径；缺省依次查找当前目录与用户配置目录",
)
_config_parent.add_argument(
    "--log-level",
    default=None,
    help="日志级别：DEBUG/INFO/WARNING/ERROR，缺省读配置",
)
_config_parent.add_argument(
    "--log-file",
    default=None,
    help="日志文件路径，缺省读配置；未设置时只输出到标准错误",
)
_config_parent.add_argument(
    "--log-format",
    default=None,
    help="日志格式：text/json，缺省读配置",
)


def _load_settings(args: argparse.Namespace, overrides: dict | None = None):
    merged = dict(overrides or {})
    if getattr(args, "log_level", None) is not None:
        merged["log_level"] = args.log_level
    if getattr(args, "log_file", None) is not None:
        merged["log_file"] = args.log_file
    if getattr(args, "log_format", None) is not None:
        merged["log_format"] = args.log_format
    settings = load_settings(getattr(args, "config", None), merged)
    setup_logging(settings)
    return settings


def _registry_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    if getattr(args, "offline", False):
        overrides["registry_offline"] = True
    if getattr(args, "registry_mirror", None) is not None:
        overrides["registry_mirror"] = args.registry_mirror
    return overrides


def _window_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    if getattr(args, "expand_window", False):
        overrides["retriever_expand_window"] = True
    if getattr(args, "window_radius", None) is not None:
        overrides["retriever_window_radius"] = int(args.window_radius)
    return overrides


def _connect(settings: LibrefSettings, *, check_same_thread: bool = True):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(settings.db_path, check_same_thread=check_same_thread)
    apply_migrations(conn)
    return conn


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    conn = _connect(settings)
    applied = applied_count(conn)
    conn.close()
    print(f"db ready at {settings.db_path} ({applied} migrations applied)")
    return 0


def applied_count(conn) -> int:
    from libref.db.migrations import MIGRATIONS, applied_versions

    return len(applied_versions(conn) & {m.version for m in MIGRATIONS})


def _build_vector(settings: LibrefSettings, gateway=None):
    """Assemble vector store and embed function via the composition root."""
    vec, embed, _gw = build_vector(settings, gateway)
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
        "render_js": bool(getattr(args, "render_js", False)),
    }


def cmd_refresh(args: argparse.Namespace) -> int:
    """刷新意图入口：复用抓取链路增量语义。"""
    settings = _load_settings(args)
    conn = _connect(settings)
    vec_store, embed = _build_vector(settings)
    scope = _scope_from_args(args)
    try:
        if getattr(args, "run_async", False):
            mgr = JobManager(conn)
            mgr.recover()
            job_id = start_refresh_job(
                mgr,
                args.library, args.version, args.start_url,
                max_pages=args.max_pages, prefilter=args.prefilter,
                scope=scope, ecosystem=getattr(args, "ecosystem", None) or "custom",
            )
            job = run_scrape_job_sync(
                mgr, settings, conn, job_id, vec_store=vec_store, embed=embed
            )
            _print_job(job)
            return 0 if job.status == "done" else 1
        service = IngestService(settings, conn, vec_store=vec_store, embed=embed)
        counts = service.refresh_version(
            args.library, args.version, args.start_url,
            max_pages=args.max_pages, prefilter=args.prefilter,
            scope=scope, ecosystem=getattr(args, "ecosystem", None) or "custom",
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """本地冒烟：起临时 HTTP 站验证发现到落库全链路，不依赖外网。"""
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import tempfile

    pages = {
        "/sitemap.xml": (
            "<?xml version=\"1.0\"?><urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
            "<url><loc>PLACE/a.html</loc></url><url><loc>PLACE/b.md</loc></url></urlset>",
            "application/xml",
        ),
        "/a.html": (
            "<html><head><title>A</title></head><body><main><h1>Guide</h1>"
            "<p>smoke install libref here</p></main></body></html>",
            "text/html",
        ),
        "/b.md": ("# Guide\n\nsmoke markdown body\n", "text/markdown"),
    }

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body, ctype = pages.get(self.path, (None, None))
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body.replace("PLACE", f"http://127.0.0.1:{self.server.server_port}").encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_HEAD(self):
            body, ctype = pages.get(self.path, (None, None))
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()

    settings = _load_settings(args)
    conn = _connect(settings)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        service = IngestService(settings, conn)
        counts = service.scrape_docs("smoke", "1.0", f"{base}/sitemap.xml")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    ok = counts.get("stored", 0) >= 1
    print("smoke: ok" if ok else "smoke: empty")
    return 0 if ok else 1


def _resolve_for_cli(settings, conn, ecosystem, library, requested_version):
    """命令行解析形态：生态加名称解析出地址与版本，失败打印分类错误。"""
    import asyncio

    from libref.crawler.http import build_async_client
    from libref.services.resolver import resolve_package

    async def _run():
        async with build_async_client(settings) as client:
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
    settings = _load_settings(args, _registry_overrides(args))
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
            _print_job(job)
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

    from libref.crawler.registry import REGISTRY_MIRRORS, RegistryError

    settings = _load_settings(args, _registry_overrides(args))
    conn = _connect(settings)
    repos_dir = settings.repos_dir
    vec_store, embed = _build_vector(settings)
    try:
        if args.source is not None:
            if args.version is None:
                print("explicit mode requires library version source", file=sys.stderr)
                return 2
            if args.ecosystem is not None and args.ecosystem.strip().lower() in ("pypi", "npm", "maven"):
                from libref.crawler.registry import normalize_name

                library = normalize_name(args.ecosystem.strip().lower(), args.library)
                ecosystem = args.ecosystem.strip().lower()
            else:
                library, ecosystem = args.library.strip(), (args.ecosystem or "custom")
            version, source = args.version, args.source
            resolved_from, registry_confirmed = "explicit", True
        else:
            try:
                async def _resolve():
                    from libref.crawler.http import build_async_client
                    from libref.services.resolver import resolve_repo

                    async with build_async_client(settings) as client:
                        return await resolve_repo(
                            conn, client, settings,
                            args.ecosystem, args.library, args.version,
                        )
                resolution = asyncio.run(_resolve())
            except ValueError as exc:
                print(f"invalid: {exc}", file=sys.stderr)
                return 2
            except RegistryError as exc:
                hint = f" hint={exc.hint}" if exc.hint else ""
                print(f"resolve failed [{exc.kind}]: {exc}{hint}", file=sys.stderr)
                return 1
            assert resolution.repo_url is not None
            library, version, source = resolution.name, resolution.version, resolution.repo_url
            ecosystem = resolution.ecosystem
            resolved_from = resolution.source
            registry_confirmed = resolution.registry_confirmed
            print(f"resolved {ecosystem}:{library} {version} -> {source} [{resolved_from}]")
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                INDEX_REPO_JOB_KIND,
                {
                    "library": library,
                    "version": version,
                    "source": source,
                    "update": bool(args.update),
                    "ecosystem": ecosystem,
                    "resolved_from": resolved_from,
                    "registry_confirmed": registry_confirmed,
                },
            )

            async def _drive():
                mgr.start_worker(build_repo_runner(settings, conn, repos_dir, vec_store, embed))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            _print_job(job)
            return 0 if job.status == "done" else 1
        try:
            counts = index_repo(
                conn,
                library,
                version,
                source,
                update=bool(args.update),
                repos_dir=repos_dir,
                ecosystem=ecosystem,
                resolved_from=resolved_from,
                registry_confirmed=registry_confirmed,
                repos_quota_mb=int(settings.repos_quota_mb),
                settings=settings, vec_store=vec_store, embed=embed,
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_mine(args: argparse.Namespace) -> int:
    import asyncio
    import json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                MINE_PATTERNS_JOB_KIND,
                mine_job_params(
                    args.library,
                    args.version,
                    ecosystem=getattr(args, "ecosystem", None),
                    minsup=args.minsup,
                    topn=args.topn,
                    maxlen=args.maxlen,
                    minlen=args.minlen,
                    include_ambiguous=bool(args.include_ambiguous),
                    cluster_threshold=args.cluster_threshold,
                    cluster_min_size=args.cluster_min_size,
                ),
            )

            async def _drive():
                mgr.start_worker(build_mine_runner(settings, conn))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            _print_job(job)
            return 0 if job.status == "done" else 1
        results = mine_patterns(
            conn,
            args.library,
            args.version,
            ecosystem=getattr(args, "ecosystem", None),
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
    import asyncio as _asyncio
    import json as _json
    settings = _load_settings(args, _window_overrides(args))
    conn = _connect(settings)
    try:
        if getattr(args, "precise", False):
            service = SearchService(conn)
            results = service.search_docs(
                args.query,
                library=args.library,
                version=args.version,
                limit=args.limit,
            )
            degraded = False
        else:
            vec_store, embed = _build_vector(settings)
            service = SearchService(conn, settings, vec_store=vec_store, embed=embed)
            results, degraded = _asyncio.run(service.search_hybrid_async(
                args.query,
                library=args.library,
                version=args.version,
                limit=args.limit,
            ))
            if degraded:
                print("(degraded: partial retrieval)", file=sys.stderr)
    finally:
        conn.close()
    if not results:
        print("(no results)")
        return 0
    for r in results:
        print(f"[{r['score']:.3f}] {r['title'] or '(untitled)'} — {r['url']}")
        print(f"    {r['snippet']}")
        if r.get("window"):
            print(f"    window: {_json.dumps(r['window'], ensure_ascii=False)[:300]}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    settings = _load_settings(args, _window_overrides(args))
    conn = _connect(settings)
    try:
        from libref.services.factory import build_gateway

        gateway = build_gateway(settings)
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
            material_limit=args.material_limit,
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


def cmd_read_window(args: argparse.Namespace) -> int:
    import json as _json
    conn = _connect(_load_settings(args))
    try:
        page = PageService(conn).read_window(
            args.url, args.sort_order, radius=args.radius,
            library=args.library, version=args.version,
        )
    finally:
        conn.close()
    if page is None:
        print(f"(window not found: {args.url} @{args.sort_order})", file=sys.stderr)
        return 1
    print(_json.dumps(page, ensure_ascii=False))
    return 0


def cmd_read_section(args: argparse.Namespace) -> int:
    import json as _json
    conn = _connect(_load_settings(args))
    try:
        page = PageService(conn).read_section(
            args.url, args.source_group_id,
            library=args.library, version=args.version,
        )
    finally:
        conn.close()
    if page is None:
        print(f"(section not found: {args.url} {args.source_group_id})", file=sys.stderr)
        return 1
    print(_json.dumps(page, ensure_ascii=False))
    return 0


def cmd_read_page(args: argparse.Namespace) -> int:
    conn = _connect(_load_settings(args))
    try:
        page = PageService(conn).read_page(
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

    conn = _connect(_load_settings(args))
    try:
        rows = MiningReadService(conn).feature_tree(
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

    conn = _connect(_load_settings(args))
    try:
        containing = args.containing.split(",") if args.containing else None
        rows = MiningReadService(conn).api_clusters(
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

    from libref.chunking import budgets_from_settings, split_markdown_dual

    budgets = budgets_from_settings(_load_settings(args))
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


def cmd_reembed(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        vec_store, embed = _build_vector(settings)
        if vec_store is None or embed is None:
            print("embedding is not configured (missing key)", file=sys.stderr)
            return 1
        out = MaintenanceService(conn, settings, vec_store=vec_store, embed=embed).reembed_library(
            args.library, args.version, ecosystem=args.ecosystem,
        )
    finally:
        conn.close()
    print(f"docs: {out['docs']} upserted: {out['upserted']}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    conn = _connect(_load_settings(args))
    try:
        svc = ReviewService(conn)
        action = args.review_action
        if action == "list":
            for r in svc.list_pending(limit=args.limit):
                print(
                    f"{r['entity_id']} {r['confidence']} {r['callee']} "
                    f"{r['caller_file']}:{r['line_start']}-{r['line_end']}"
                )
            return 0
        if action == "approve":
            svc.approve(args.entity_id)
        elif action == "conflict":
            svc.mark_conflict(args.entity_id)
        elif action == "reopen":
            svc.reopen(args.entity_id)
        else:
            print(f"unknown review action: {action}", file=sys.stderr)
            return 2
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print("ok")
    return 0


def _print_job(job) -> None:
    print(
        f"job {job.id} {job.status}"
        + (f" error={job.error}" if job.error else "")
    )
    result = getattr(job, "result", None)
    if result:
        import json as _json
        print(f"result: {_json.dumps(result, ensure_ascii=False)[:2000]}")
    if getattr(job, "finished_at", None):
        print(f"finished_at: {job.finished_at}")


def cmd_job_show(args: argparse.Namespace) -> int:
    import json as _json
    conn = _connect(_load_settings(args))
    try:
        mgr = JobManager(conn)
        if getattr(args, "list", False) or getattr(args, "job_id", None) is None:
            for job in mgr.list_recent(limit=args.limit):
                print(f"{job.id} {job.kind} {job.status} {job.finished_at or ''}")
            return 0
        job = mgr.get(args.job_id)
        if job is None:
            print(f"(unknown job: {args.job_id})", file=sys.stderr)
            return 1
        _print_job(job)
        if args.full and job.result:
            print(_json.dumps(job.result, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def cmd_purge(args: argparse.Namespace) -> int:
    from libref.services.retention import purge_version
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        vec_store, _embed = _build_vector(settings)
        out = purge_version(
            conn,
            args.library,
            args.version,
            ecosystem=getattr(args, "ecosystem", None),
            repos_dir=settings.repos_dir,
            snapshot_root=settings.snapshot_dir,
            vec_store=vec_store,
        )
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    for key, value in out.items():
        print(f"{key}: {value}")
    return 0


def cmd_deletions(args: argparse.Namespace) -> int:
    import json as _json
    from libref.services.retention import list_deletions, prune_expired_logs
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        if getattr(args, "prune", False):
            ttl = getattr(args, "ttl_days", None)
            if ttl is None:
                ttl = float(settings.deletion_log_ttl_days)
            removed = prune_expired_logs(conn, float(ttl))
            print(f"pruned: {removed}")
            return 0
        rows = list_deletions(conn, limit=int(getattr(args, "limit", 50)))
        if not rows:
            print("(no deletions)")
            return 0
        for row in rows:
            print(f"{row['id']} {row['ecosystem']}:{row['library']} {row['version']} {row['reason']} {row['created_at']}")
            if getattr(args, "full", False):
                print(f"    {_json.dumps(row['detail'], ensure_ascii=False)[:1000]}")
        return 0
    finally:
        conn.close()


def cmd_config_show(args: argparse.Namespace) -> int:
    import json as _json
    settings = _load_settings(args)
    path = resolve_config_file(getattr(args, "config", None))
    print(_json.dumps({
        "file": str(path) if path is not None else "",
        "fingerprint": settings_fingerprint(settings),
        "settings": redacted_dict(settings),
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from libref.mcp_server import build_mcp_server, check_startup

    settings = _load_settings(args)
    if args.http:
        host = args.host or settings.mcp_host
        try:
            check_startup(
                host,
                require_auth=bool(settings.mcp_require_auth),
                token=settings.mcp_token,
            )
        except ValueError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
    # MCPServer 在 worker 线程执行工具，连接需跨线程复用。
    conn = _connect(settings, check_same_thread=False)
    mcp = build_mcp_server(conn, settings)
    if args.http:
        host = args.host or settings.mcp_host
        port = args.port or settings.mcp_port
        path = args.path or settings.mcp_path
        mcp.run("streamable-http", host=host, port=port,
                streamable_http_path=path)  # 阻塞直到进程退出
    else:
        mcp.run("stdio")  # 阻塞直到 stdio 关闭
    conn.close()
    return 0


def cmd_serve_web(args: argparse.Namespace) -> int:
    from libref.mcp_server.auth import check_startup

    settings = _load_settings(args)
    host = args.host or settings.web_host
    port = args.port or settings.web_port
    try:
        check_startup(
            host,
            require_auth=bool(settings.mcp_require_auth),
            token=settings.mcp_token,
        )
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    from libref.web import build_web_app

    conn = _connect(settings, check_same_thread=False)
    from libref.services.factory import build_gateway

    gateway = build_gateway(settings)
    vec_store, embed = _build_vector(settings, gateway)
    app = build_web_app(conn, settings, vec_store=vec_store, embed=embed,
                        rerank=gateway.rerank_fn(), chat=gateway.chat_fn())
    uvicorn.run(app, host=host, port=port, log_level="warning")
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libref", description="libref 文档知识库", parents=[_config_parent])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="初始化数据库（应用迁移）", parents=[_config_parent]).set_defaults(
        func=cmd_init_db
    )

    p_ingest = sub.add_parser("ingest", help="抓取文档站并入库", parents=[_config_parent])
    p_ingest.add_argument("library", nargs="?", help="显式形态为库名，解析形态为包名")
    p_ingest.add_argument(
        "version", nargs="?", default=None, help="显式形态必填，解析形态缺省取稳定版"
    )
    p_ingest.add_argument(
        "start_url", nargs="?", default=None, help="显式形态必填，与 --ecosystem 互斥"
    )
    p_ingest.add_argument("--ecosystem", default=None, help="按名称解析：pypi|npm|maven，java 坐标形如 group:artifact")
    p_ingest.add_argument("--offline", action="store_true", help="只读缓存，不请求网络")
    p_ingest.add_argument(
        "--registry-mirror",
        default=None,
        help=f"注册表镜像预设（{', '.join(sorted(REGISTRY_MIRRORS))}），缺省读配置",
    )
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
    p_ingest.add_argument(
        "--render-js", action="store_true", help="疑似渲染页尝试浏览器降级"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_refresh = sub.add_parser("refresh", help="刷新版本文档并入库", parents=[_config_parent])
    p_refresh.add_argument("library")
    p_refresh.add_argument("version")
    p_refresh.add_argument("start_url")
    p_refresh.add_argument("--ecosystem", default=None)
    p_refresh.add_argument("--max-pages", type=int, default=None)
    p_refresh.add_argument("--prefilter", action="store_true")
    p_refresh.add_argument("--render-js", action="store_true")
    p_refresh.add_argument(
        "--async", dest="run_async", action="store_true", help="经 jobs 队列执行"
    )
    p_refresh.set_defaults(func=cmd_refresh)

    sub.add_parser("smoke-local", help="本地冒烟：临时 HTTP 站全链路", parents=[_config_parent]).set_defaults(func=cmd_smoke)

    p_search = sub.add_parser("search", help="默认混合检索", parents=[_config_parent])
    p_search.add_argument("query")
    p_search.add_argument("--library", default=None)
    p_search.add_argument("--version", default=None)
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--precise", action="store_true", help="只走全文单路")
    p_search.add_argument("--expand-window", action="store_true", help="命中后扩展前后窗口")
    p_search.add_argument("--window-radius", type=int, default=None)
    p_search.set_defaults(func=cmd_search)

    p_answer = sub.add_parser("answer", help="混合检索加问答生成", parents=[_config_parent])
    p_answer.add_argument("query")
    p_answer.add_argument("--library", default=None)
    p_answer.add_argument("--version", default=None)
    p_answer.add_argument("--limit", type=int, default=5)
    p_answer.add_argument("--expand-window", action="store_true", help="命中后扩展前后窗口")
    p_answer.add_argument("--window-radius", type=int, default=None)
    p_answer.add_argument("--material-limit", type=int, default=None, help="问答材料截断长度，缺省读配置")
    p_answer.set_defaults(func=cmd_answer)

    p_read = sub.add_parser("read-page", help="按 URL 读取整页文档", parents=[_config_parent])
    p_read.add_argument("url")
    p_read.add_argument("--library", default=None)
    p_read.add_argument("--version", default=None)
    p_read.set_defaults(func=cmd_read_page)

    p_window = sub.add_parser("read-window", help="按序号取前后窗口", parents=[_config_parent])
    p_window.add_argument("url")
    p_window.add_argument("sort_order", type=int)
    p_window.add_argument("--radius", type=int, default=1)
    p_window.add_argument("--library", default=None)
    p_window.add_argument("--version", default=None)
    p_window.set_defaults(func=cmd_read_window)

    p_section = sub.add_parser("read-section", help="按源分组取整节", parents=[_config_parent])
    p_section.add_argument("url")
    p_section.add_argument("source_group_id")
    p_section.add_argument("--library", default=None)
    p_section.add_argument("--version", default=None)
    p_section.set_defaults(func=cmd_read_section)

    p_mcp = sub.add_parser("serve-mcp", help="启动 MCP 服务（默认 stdio）", parents=[_config_parent])
    p_mcp.add_argument("--http", action="store_true",
                       help="改用 Streamable HTTP 传输（默认只绑本地回环）")
    p_mcp.add_argument("--host", default=None, help="HTTP 监听地址")
    p_mcp.add_argument("--port", type=int, default=None, help="HTTP 监听端口")
    p_mcp.add_argument("--path", default=None, help="HTTP 端点路径")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    p_web = sub.add_parser("serve-web", help="启动 Web 服务（默认只绑本地回环）", parents=[_config_parent])
    p_web.add_argument("--host", default=None, help="HTTP 监听地址")
    p_web.add_argument("--port", type=int, default=None, help="HTTP 监听端口")
    p_web.set_defaults(func=cmd_serve_web)

    p_repo = sub.add_parser("index-repo", help="索引源码仓库并落调用实例", parents=[_config_parent])
    p_repo.add_argument("library", help="显式形态为库名，解析形态为包名")
    p_repo.add_argument("version", nargs="?", default=None, help="显式形态必填，解析形态缺省取稳定版")
    p_repo.add_argument("source", nargs="?", default=None, help="显式形态为远端地址或本地路径，缺省按名解析")
    p_repo.add_argument("--ecosystem", default=None, help="按名称解析：pypi|npm|maven，缺省为 custom 显式")
    p_repo.add_argument("--offline", action="store_true", help="只读缓存，不请求网络")
    p_repo.add_argument(
        "--registry-mirror",
        default=None,
        help="注册表镜像预设，缺省读配置",
    )
    p_repo.add_argument("--update", action="store_true", help="已存在仓库时拉取更新")
    p_repo.add_argument(
        "--async", dest="run_async", action="store_true", help="经 jobs 队列执行"
    )
    p_repo.set_defaults(func=cmd_index_repo)

    p_mine = sub.add_parser("mine", help="挖掘闭合调用序列模式", parents=[_config_parent])
    p_mine.add_argument("library")
    p_mine.add_argument("version")
    p_mine.add_argument("--ecosystem", default=None, help="库所属生态，缺省跨生态定位")
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

    p_tree = sub.add_parser("feature-tree", help="按前缀浏览分层特征", parents=[_config_parent])
    p_tree.add_argument("library")
    p_tree.add_argument("version")
    p_tree.add_argument("--prefix", default=None)
    p_tree.add_argument("--limit", type=int, default=100)
    p_tree.set_defaults(func=cmd_feature_tree)

    p_clusters = sub.add_parser("clusters", help="浏览接口分组", parents=[_config_parent])
    p_clusters.add_argument("library")
    p_clusters.add_argument("version")
    p_clusters.add_argument("--containing", default=None, help="逗号分隔的必含接口")
    p_clusters.add_argument("--limit", type=int, default=50)
    p_clusters.set_defaults(func=cmd_clusters)

    p_dump = sub.add_parser("dump-chunks", help="预览分块：文件到分块的行号字节标题", parents=[_config_parent])
    group = p_dump.add_mutually_exclusive_group(required=True)
    group.add_argument("--markdown", default=None, help="Markdown 文档路径")
    group.add_argument("--source", default=None, help="源码文件路径")
    p_dump.add_argument("--url", default=None, help="文档块地址标注")
    p_dump.set_defaults(func=cmd_dump_chunks)

    p_reembed = sub.add_parser("reembed", help="按真源全量重灌整版本向量", parents=[_config_parent])
    p_reembed.add_argument("library")
    p_reembed.add_argument("version")
    p_reembed.add_argument("--ecosystem", default=None)
    p_reembed.set_defaults(func=cmd_reembed)

    p_review = sub.add_parser("review", help="审核待定调用实例", parents=[_config_parent])
    review_sub = p_review.add_subparsers(dest="review_action", required=True)
    review_sub.add_parser("list", help="列出待定项").add_argument(
        "--limit", type=int, default=50,
    )
    for _name in ("approve", "conflict", "reopen"):
        _p = review_sub.add_parser(_name)
        _p.add_argument("entity_id", type=int)
    p_review.set_defaults(func=cmd_review)

    p_job = sub.add_parser("job-show", help="查看任务审计结论", parents=[_config_parent])
    p_job.add_argument("job_id", type=int, nargs="?", default=None)
    p_job.add_argument("--list", action="store_true", help="列出最近任务")
    p_job.add_argument("--limit", type=int, default=20)
    p_job.add_argument("--full", action="store_true", help="打印完整结论")
    p_job.set_defaults(func=cmd_job_show)

    p_config = sub.add_parser("config-show", help="打印脱敏生效配置", parents=[_config_parent])
    p_config.set_defaults(func=cmd_config_show)

    p_purge = sub.add_parser("purge", help="整版删除并记日志", parents=[_config_parent])
    p_purge.add_argument("library")
    p_purge.add_argument("version")
    p_purge.add_argument("--ecosystem", default=None)
    p_purge.set_defaults(func=cmd_purge)

    p_del = sub.add_parser("deletions", help="查看删除日志/清理过期记录", parents=[_config_parent])
    p_del.add_argument("--limit", type=int, default=50)
    p_del.add_argument("--full", action="store_true", help="打印摘要详情")
    p_del.add_argument("--prune", action="store_true", help="按保留期清理过期记录")
    p_del.add_argument("--ttl-days", type=float, default=None, help="缺省读配置 deletion_log_ttl_days")
    p_del.set_defaults(func=cmd_deletions)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
