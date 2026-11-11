"""Command-line exit: only argument parsing plus service calls plus result printing."""

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
from libref.services.code.code_ingest import (
    INDEX_REPO_JOB_KIND,
    build_repo_runner,
    index_repo,
)
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_refresh import (
    MINE_PATTERNS_JOB_KIND,
    build_mine_runner,
    mine_job_params,
    mine_patterns,
)
from libref.services.docs.answer import answer_query
from libref.services.docs.ingest import (
    IngestService,
    run_scrape_job_sync,
    start_refresh_job,
    start_scrape_job,
)
from libref.services.docs.pages import PageService
from libref.services.factory import (
    build_discovery_service,
    build_ingest_service,
    build_maintenance_service,
    build_search_service,
    build_vector,
)
from libref.services.infra.jobs import JobManager
from libref.services.ops.review import ReviewService

_config_parent = argparse.ArgumentParser(add_help=False)
_config_parent.add_argument(
    "--config",
    default=None,
    help="Config file path; by default looked up in the current directory, then the user config directory",
)
_config_parent.add_argument(
    "--log-level",
    default=None,
    help="Log level: DEBUG/INFO/WARNING/ERROR; defaults to the config value",
)
_config_parent.add_argument(
    "--log-file",
    default=None,
    help="Log file path; defaults to the config value; when unset, only stderr is used",
)
_config_parent.add_argument(
    "--log-format",
    default=None,
    help="Log format: text/json; defaults to the config value",
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
        "render_wait_for": getattr(args, "render_wait_for", None),
        "render_timeout_ms": getattr(args, "render_timeout_ms", None),
        "follow_links": bool(getattr(args, "follow_links", False)),
        "crawl_query": getattr(args, "crawl_query", None),
        "score_threshold": getattr(args, "score_threshold", None),
        "bypass_cache": bool(getattr(args, "bypass_cache", False)),
    }


def cmd_refresh(args: argparse.Namespace) -> int:
    """Refresh-intent entry: reuses the scrape path's incremental semantics."""
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
                args.library,
                args.version,
                args.start_url,
                max_pages=args.max_pages,
                prefilter=args.prefilter,
                scope=scope,
                ecosystem=getattr(args, "ecosystem", None) or "custom",
            )
            job = run_scrape_job_sync(
                mgr, settings, conn, job_id, vec_store=vec_store, embed=embed
            )
            _print_job(job)
            return 0 if job.status == "done" else 1
        service, _gw = build_ingest_service(
            conn, settings, vec_store=vec_store, embed=embed
        )
        counts = service.refresh_version(
            args.library,
            args.version,
            args.start_url,
            max_pages=args.max_pages,
            prefilter=args.prefilter,
            scope=scope,
            ecosystem=getattr(args, "ecosystem", None) or "custom",
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """Local smoke test: spin up a temporary HTTP site to validate the full chain from discovery to storage, no external network needed."""
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    pages = {
        "/sitemap.xml": (
            (
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>PLACE/a.html</loc></url><url><loc>PLACE/b.md</loc></url></urlset>"
            ),
            "application/xml",
        ),
        "/a.html": (
            (
                "<html><head><title>A</title></head><body><main><h1>Guide</h1>"
                "<p>smoke install libref here</p></main></body></html>"
            ),
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
            data = body.replace(
                "PLACE", f"http://127.0.0.1:{self.server.server_port}"
            ).encode()
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


def _resolve_for_cli(
    settings, conn, ecosystem, library, requested_version, include_prerelease=False
):
    """CLI resolution form: ecosystem plus name resolve to address and version; on failure print the classified error."""
    import asyncio

    from libref.crawler.http import build_async_client
    from libref.services.registry.resolver import resolve

    async def _run():
        async with build_async_client(settings) as client:
            return await resolve(
                conn,
                client,
                settings,
                ecosystem,
                library,
                requested_version,
                "docs",
                include_prerelease,
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
                    bool(getattr(args, "include_prerelease", False)),
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
        service, _gw = build_ingest_service(
            conn, settings, vec_store=vec_store, embed=embed
        )
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

    from libref.crawler.registry import RegistryError

    settings = _load_settings(args, _registry_overrides(args))
    conn = _connect(settings)
    repos_dir = settings.repos_dir
    vec_store, embed = _build_vector(settings)
    try:
        if args.source is not None:
            if args.version is None:
                print("explicit mode requires library version source", file=sys.stderr)
                return 2
            from libref.crawler.registry import (
                SUPPORTED_ECOSYSTEMS,
                canonical_ecosystem,
                normalize_name,
            )

            eco_name = (
                canonical_ecosystem(args.ecosystem)
                if args.ecosystem is not None
                else None
            )
            if eco_name in SUPPORTED_ECOSYSTEMS:
                library = normalize_name(eco_name, args.library)
                ecosystem = eco_name
            else:
                library, ecosystem = args.library.strip(), (args.ecosystem or "custom")
            version, source = args.version, args.source
            resolved_from, registry_confirmed = "explicit", True
        else:
            try:

                async def _resolve():
                    from libref.crawler.http import build_async_client
                    from libref.services.registry.resolver import resolve

                    async with build_async_client(settings) as client:
                        return await resolve(
                            conn,
                            client,
                            settings,
                            args.ecosystem,
                            args.library,
                            args.version,
                            "repo",
                            bool(getattr(args, "include_prerelease", False)),
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
            library, version, source = (
                resolution.name,
                resolution.version,
                resolution.repo_url,
            )
            ecosystem = resolution.ecosystem
            resolved_from = resolution.source
            registry_confirmed = resolution.registry_confirmed
            print(
                f"resolved {ecosystem}:{library} {version} -> {source} [{resolved_from}]"
            )
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
                mgr.start_worker(
                    build_repo_runner(settings, conn, repos_dir, vec_store, embed)
                )
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
                settings=settings,
                vec_store=vec_store,
                embed=embed,
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
        service, _gw = build_search_service(conn, settings)
        mode = getattr(args, "mode", "hybrid")
        results, degraded = _asyncio.run(
            service.search_with_mode(
                args.query,
                mode,
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
            )
        )
        if degraded:
            print("(degraded: partial retrieval)", file=sys.stderr)
        if not results:
            print("(no results)")
            if getattr(args, "suggest", False):
                return _print_suggestions(conn, settings, args)
            return 0
    finally:
        conn.close()
    for r in results:
        print(f"[{r['score']:.3f}] {r['title'] or '(untitled)'} — {r['url']}")
        print(f"    {r['snippet']}")
        if r.get("window"):
            print(f"    window: {_json.dumps(r['window'], ensure_ascii=False)[:300]}")
    return 0


def _print_suggestions(conn, settings, args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import json as _json

    from libref.crawler.registry import RegistryError

    try:
        candidates = _asyncio.run(
            build_discovery_service(conn, settings).search_async(
                getattr(args, "library", None) or args.query,
                getattr(args, "ecosystem", None),
                args.limit,
                include_prerelease=bool(getattr(args, "include_prerelease", False)),
            )
        )
    except ValueError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 2
    except RegistryError as exc:
        print(f"discovery failed [{exc.kind}]: {exc}", file=sys.stderr)
        return 1
    if not candidates:
        print("(no suggestions)")
        return 0
    print("suggestions:")
    for c in candidates:
        print(_json.dumps(c, ensure_ascii=False))
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        return _print_suggestions(conn, settings, args)
    finally:
        conn.close()


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
            ecosystem=getattr(args, "ecosystem", None),
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


def cmd_read(args: argparse.Namespace) -> int:
    import json as _json

    conn = _connect(_load_settings(args))
    try:
        try:
            page = PageService(conn).read_doc(
                args.url,
                args.mode,
                sort_order=args.sort_order,
                radius=args.radius,
                source_group_id=args.source_group_id,
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    if page is None:
        print(f"(not found: {args.url})", file=sys.stderr)
        return 1
    if args.mode == "page":
        print(page["content"])
    else:
        print(_json.dumps(page, ensure_ascii=False))
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    import json

    from libref.services.registry.libraries import LibraryService

    conn = _connect(_load_settings(args))
    try:
        rows = LibraryService(conn).catalog(
            getattr(args, "library", None),
            getattr(args, "ecosystem", None),
        )
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_induce_labels(args: argparse.Namespace) -> int:
    import asyncio
    import json as _json

    from libref.services.code.mining_refresh import (
        INDUCE_LABELS_JOB_KIND,
        build_induce_runner,
        induce_job_params,
        induce_labels,
    )

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        if args.run_async:
            mgr = JobManager(conn)
            mgr.recover()
            job_id = mgr.submit(
                INDUCE_LABELS_JOB_KIND,
                induce_job_params(
                    args.library,
                    args.version,
                    ecosystem=getattr(args, "ecosystem", None),
                    topn=args.topn,
                ),
            )

            async def _drive():
                mgr.start_worker(build_induce_runner(settings, conn))
                try:
                    return await mgr.wait_for(job_id)
                finally:
                    mgr.stop_worker()

            job = asyncio.run(_drive())
            _print_job(job)
            return 0 if job.status == "done" else 1
        out = asyncio.run(
            induce_labels(
                conn,
                settings,
                args.library,
                args.version,
                ecosystem=getattr(args, "ecosystem", None),
                topn=args.topn,
            )
        )
    finally:
        conn.close()
    print(_json.dumps(out, ensure_ascii=False))
    return 0


def cmd_api_insight(args: argparse.Namespace) -> int:
    import json

    conn = _connect(_load_settings(args))
    try:
        try:
            apis = args.apis.split(",") if getattr(args, "apis", None) else None
            rows = MiningReadService(conn).api_insight(
                args.view,
                getattr(args, "api", None),
                apis=apis,
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_pattern_insight(args: argparse.Namespace) -> int:
    import json

    conn = _connect(_load_settings(args))
    try:
        containing = (
            args.containing.split(",") if getattr(args, "containing", None) else None
        )
        pattern = args.pattern.split(",") if getattr(args, "pattern", None) else None
        try:
            rows = MiningReadService(conn).pattern_insight(
                args.view,
                pattern=pattern,
                containing=containing,
                prefix=getattr(args, "prefix", None),
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
                per_file_limit=getattr(args, "per_file_limit", None),
                dedup=None if getattr(args, "no_dedup", False) is False else False,
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    import asyncio
    import json as _json

    from libref.crawler.http import build_async_client
    from libref.services.registry.resolver import resolve

    settings = _load_settings(args)
    conn = _connect(settings)
    try:

        async def _run():
            async with build_async_client(settings) as client:
                return await resolve(
                    conn,
                    client,
                    settings,
                    args.ecosystem,
                    args.name,
                    getattr(args, "version", None),
                    getattr(args, "target", "docs"),
                    bool(getattr(args, "include_prerelease", False)),
                )

        try:
            resolution = asyncio.run(_run())
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        except RegistryError as exc:
            hint = f" hint={exc.hint}" if exc.hint else ""
            print(f"resolve failed [{exc.kind}]: {exc}{hint}", file=sys.stderr)
            return 1
    finally:
        conn.close()
    print(_json.dumps(resolution.model_dump(), ensure_ascii=False))
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
    for source_chunk in split.chunks:
        print(
            f"[{source_chunk.chunk_id}] {source_chunk.qual} kind={source_chunk.span_kind} "
            f"bytes={source_chunk.start_byte}-{source_chunk.end_byte} merged={source_chunk.merged_quals} "
            f"overlap_from={source_chunk.overlap_from}"
        )
        print(f"    {source_chunk.content[:160].replace(chr(10), ' ')}")
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
        svc, _gw = build_maintenance_service(
            conn, settings, vec_store=vec_store, embed=embed
        )
        out = svc.reembed_library(
            args.library,
            args.version,
            ecosystem=args.ecosystem,
        )
    finally:
        conn.close()
    print(f"docs: {out['docs']} upserted: {out['upserted']}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    import asyncio as _asyncio

    from libref.services.ops.maintenance import reconcile_version
    from libref.services.registry.libraries import mining_version_ids

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        vec_store, embed = _build_vector(settings)
        if vec_store is None or embed is None:
            print("embedding is not configured (missing key)", file=sys.stderr)
            return 1
        vids = mining_version_ids(conn, args.library, args.version, args.ecosystem)
        if len(vids) != 1:
            print(
                "invalid: library version must resolve to a single version",
                file=sys.stderr,
            )
            return 1
        out = _asyncio.run(reconcile_version(conn, settings, vec_store, embed, vids[0]))
    finally:
        conn.close()
    print(
        f"sqlite: {out['sqlite_embeddable']} vector_before: {out['vector_before']} "
        f"missing: {out['missing']} orphans: {out['orphans']} "
        f"upserted: {out['upserted']} rebuilt: {out['rebuilt']}"
    )
    return 0


def cmd_fts_rebuild(args: argparse.Namespace) -> int:
    from libref.services.ops.maintenance import rebuild_fts

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        out = rebuild_fts(conn)
    finally:
        conn.close()
    print(
        f"documents: {out['documents']} sampled: {out['sampled']} "
        f"missing: {out['missing']} in_sync: {out['in_sync']}"
    )
    return 0 if out["in_sync"] else 1


def cmd_fts_check(args: argparse.Namespace) -> int:
    from libref.services.ops.maintenance import check_fts

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        out = check_fts(conn)
    finally:
        conn.close()
    print(
        f"documents: {out['documents']} triggers: {out['triggers']} "
        f"sampled: {out['sampled']} missing: {out['missing']} "
        f"in_sync: {out['in_sync']}"
    )
    return 0 if out["in_sync"] else 1


def cmd_review(args: argparse.Namespace) -> int:
    conn = _connect(_load_settings(args))
    try:
        svc = ReviewService(conn)
        action = args.review_action
        try:
            if action == "list":
                result = svc.apply(action, limit=args.limit)
            else:
                result = svc.apply(action, args.entity_id)
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 1
        if action == "list":
            for r in result:
                print(
                    f"{r['entity_id']} {r['confidence']} {r['callee']} "
                    f"{r['caller_file']}:{r['line_start']}-{r['line_end']}"
                )
            return 0
    finally:
        conn.close()
    print("ok")
    return 0


def _print_job(job) -> None:
    print(f"job {job.id} {job.status}" + (f" error={job.error}" if job.error else ""))
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
            out = mgr.show(None, limit=args.limit)
            for job in out["jobs"]:
                print(
                    f"{job['id']} {job['kind']} {job['status']} {job['finished_at'] or ''}"
                )
            return 0
        out = mgr.show(args.job_id)
        if "error" in out and "id" not in out:
            print(f"(unknown job: {args.job_id})", file=sys.stderr)
            return 1
        print(
            f"job {out['id']} {out['status']}"
            + (f" error={out['error']}" if out.get("error") else "")
        )
        if out.get("result"):
            print(f"result: {_json.dumps(out['result'], ensure_ascii=False)[:2000]}")
            if args.full:
                print(_json.dumps(out["result"], ensure_ascii=False))
        if out.get("finished_at"):
            print(f"finished_at: {out['finished_at']}")
        return 0
    finally:
        conn.close()


def cmd_purge(args: argparse.Namespace) -> int:
    from libref.services.ops.retention import purge_version

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

    from libref.services.ops.retention import list_deletions, prune_expired_logs

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
            print(
                f"{row['id']} {row['ecosystem']}:{row['library']} {row['version']} {row['reason']} {row['created_at']}"
            )
            if getattr(args, "full", False):
                print(f"    {_json.dumps(row['detail'], ensure_ascii=False)[:1000]}")
        return 0
    finally:
        conn.close()


def cmd_config_show(args: argparse.Namespace) -> int:
    import json as _json

    settings = _load_settings(args)
    path = resolve_config_file(getattr(args, "config", None))
    print(
        _json.dumps(
            {
                "file": str(path) if path is not None else "",
                "fingerprint": settings_fingerprint(settings),
                "settings": redacted_dict(settings),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
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
    # MCPServer runs tools in a worker thread; the connection must be reusable across threads.
    conn = _connect(settings, check_same_thread=False)
    mcp = build_mcp_server(conn, settings)
    if args.http:
        host = args.host or settings.mcp_host
        port = args.port or settings.mcp_port
        path = args.path or settings.mcp_path
        mcp.run(
            "streamable-http", host=host, port=port, streamable_http_path=path
        )  # blocks until the process exits
    else:
        mcp.run("stdio")  # blocks until stdio closes
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
    app = build_web_app(
        conn,
        settings,
        vec_store=vec_store,
        embed=embed,
        rerank=gateway.rerank_fn(),
        chat=gateway.chat_fn(),
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libref",
        description="libref documentation knowledge base",
        parents=[_config_parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init-db",
        help="Initialize the database (apply migrations)",
        parents=[_config_parent],
    ).set_defaults(func=cmd_init_db)

    p_ingest = sub.add_parser(
        "ingest",
        help="Crawl a documentation site and store it",
        parents=[_config_parent],
    )
    p_ingest.add_argument(
        "library",
        nargs="?",
        help="Library name in explicit form, package name in resolved form",
    )
    p_ingest.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Required in explicit form; defaults to the stable version in resolved form",
    )
    p_ingest.add_argument(
        "start_url",
        nargs="?",
        default=None,
        help="Required in explicit form; mutually exclusive with --ecosystem",
    )
    p_ingest.add_argument(
        "--ecosystem",
        default=None,
        help="Resolve by name: pypi|npm|maven|go|crates|nuget; Java coordinates look like group:artifact",
    )
    p_ingest.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Resolve to the newest version including prereleases",
    )
    p_ingest.add_argument(
        "--offline",
        action="store_true",
        help="Read cache only; do not touch the network",
    )
    p_ingest.add_argument(
        "--registry-mirror",
        default=None,
        help=f"Registry mirror preset ({', '.join(sorted(REGISTRY_MIRRORS))}); defaults to the config value",
    )
    p_ingest.add_argument("--max-pages", type=int, default=None)
    p_ingest.add_argument(
        "--prefilter",
        action="store_true",
        help="Prefilter URLs with a liveness HEAD check",
    )
    p_ingest.add_argument(
        "--async",
        dest="run_async",
        action="store_true",
        help="Run via the jobs queue (submit -> in-process worker consumes -> results written back to the jobs table)",
    )
    p_ingest.add_argument(
        "--allow-cross-site", action="store_true", help="Allow crawling across sites"
    )
    p_ingest.add_argument(
        "--allow-subdomains", action="store_true", help="Allow same-domain subdomains"
    )
    p_ingest.add_argument(
        "--path-prefix",
        action="append",
        default=None,
        help="Accept only paths under this prefix; repeatable",
    )
    p_ingest.add_argument(
        "--path-suffix",
        action="append",
        default=None,
        help="Accept only paths with this suffix; repeatable",
    )
    p_ingest.add_argument(
        "--include-pattern",
        action="append",
        default=None,
        help="Regex allowlist; repeatable",
    )
    p_ingest.add_argument(
        "--exclude-pattern",
        action="append",
        default=None,
        help="Regex denylist; repeatable",
    )
    p_ingest.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum depth relative to the start URL",
    )
    p_ingest.add_argument(
        "--render-js",
        action="store_true",
        help="Fall back to the browser for pages that look JS-rendered",
    )
    p_ingest.add_argument(
        "--render-wait-for",
        default=None,
        help="CSS selector the browser waits for before returning HTML",
    )
    p_ingest.add_argument("--render-timeout-ms", type=int, default=None)
    p_ingest.add_argument(
        "--follow-links",
        action="store_true",
        help="Follow in-page links breadth-first within scope budget",
    )
    p_ingest.add_argument(
        "--crawl-query",
        default=None,
        help="Rank crawl targets by URL relevance to this query",
    )
    p_ingest.add_argument("--score-threshold", type=float, default=None)
    p_ingest.add_argument(
        "--bypass-cache",
        action="store_true",
        help="Skip conditional headers and refetch without cache revalidation",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_refresh = sub.add_parser(
        "refresh",
        help="Refresh a version's docs and store them",
        parents=[_config_parent],
    )
    p_refresh.add_argument("library")
    p_refresh.add_argument("version")
    p_refresh.add_argument("start_url")
    p_refresh.add_argument("--ecosystem", default=None)
    p_refresh.add_argument("--max-pages", type=int, default=None)
    p_refresh.add_argument("--prefilter", action="store_true")
    p_refresh.add_argument(
        "--allow-cross-site", action="store_true", help="Allow crawling across sites"
    )
    p_refresh.add_argument(
        "--allow-subdomains",
        action="store_true",
        help="Allow same-domain subdomains",
    )
    p_refresh.add_argument(
        "--path-prefix",
        action="append",
        default=None,
        help="Accept only paths under this prefix; repeatable",
    )
    p_refresh.add_argument(
        "--path-suffix",
        action="append",
        default=None,
        help="Accept only paths with this suffix; repeatable",
    )
    p_refresh.add_argument(
        "--include-pattern",
        action="append",
        default=None,
        help="Regex allowlist; repeatable",
    )
    p_refresh.add_argument(
        "--exclude-pattern",
        action="append",
        default=None,
        help="Regex denylist; repeatable",
    )
    p_refresh.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum depth relative to the start URL",
    )
    p_refresh.add_argument("--render-js", action="store_true")
    p_refresh.add_argument("--render-wait-for", default=None)
    p_refresh.add_argument("--render-timeout-ms", type=int, default=None)
    p_refresh.add_argument("--follow-links", action="store_true")
    p_refresh.add_argument("--crawl-query", default=None)
    p_refresh.add_argument("--score-threshold", type=float, default=None)
    p_refresh.add_argument(
        "--bypass-cache",
        action="store_true",
        help="Skip conditional headers and refetch without cache revalidation",
    )
    p_refresh.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_refresh.set_defaults(func=cmd_refresh)

    sub.add_parser(
        "smoke-local",
        help="Local smoke test: full chain against a temporary HTTP site",
        parents=[_config_parent],
    ).set_defaults(func=cmd_smoke)

    p_search = sub.add_parser(
        "search", help="Default hybrid retrieval", parents=[_config_parent]
    )
    p_search.add_argument("query")
    p_search.add_argument("--library", default=None)
    p_search.add_argument("--version", default=None)
    p_search.add_argument("--ecosystem", default=None)
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument(
        "--mode",
        default="hybrid",
        choices=["hybrid", "precise"],
        help="hybrid fused recall by default, precise for exact full-text",
    )
    p_search.add_argument(
        "--expand-window",
        action="store_true",
        help="Expand the surrounding window on hits",
    )
    p_search.add_argument("--window-radius", type=int, default=None)
    p_search.add_argument(
        "--suggest",
        action="store_true",
        help="Suggest unindexed libraries from registries when local hits are empty",
    )
    p_search.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Include prerelease versions when ranking suggestion candidates",
    )
    p_search.set_defaults(func=cmd_search)

    p_discover = sub.add_parser(
        "discover",
        help="Fuzzy discovery across registries (pypi/npm/maven/go/crates/nuget; language aliases accepted)",
        parents=[_config_parent],
    )
    p_discover.add_argument("query")
    p_discover.add_argument("--library", default=None)
    p_discover.add_argument("--ecosystem", default=None)
    p_discover.add_argument("--limit", type=int, default=10)
    p_discover.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Include prerelease versions when ranking candidates",
    )
    p_discover.set_defaults(func=cmd_discover)

    p_answer = sub.add_parser(
        "answer",
        help="Hybrid retrieval plus grounded answer generation",
        parents=[_config_parent],
    )
    p_answer.add_argument("query")
    p_answer.add_argument("--library", default=None)
    p_answer.add_argument("--version", default=None)
    p_answer.add_argument("--ecosystem", default=None)
    p_answer.add_argument("--limit", type=int, default=5)
    p_answer.add_argument(
        "--expand-window",
        action="store_true",
        help="Expand the surrounding window on hits",
    )
    p_answer.add_argument("--window-radius", type=int, default=None)
    p_answer.add_argument(
        "--material-limit",
        type=int,
        default=None,
        help="Answer material truncation length; defaults to the config value",
    )
    p_answer.set_defaults(func=cmd_answer)

    p_read = sub.add_parser(
        "read", help="Read a document by URL", parents=[_config_parent]
    )
    p_read.add_argument("url")
    p_read.add_argument(
        "--mode",
        default="page",
        choices=["page", "window", "section"],
        help="page for full content, window for neighbors, section for whole group",
    )
    p_read.add_argument("--sort-order", type=int, default=None)
    p_read.add_argument("--radius", type=int, default=1)
    p_read.add_argument("--source-group-id", default=None)
    p_read.add_argument("--library", default=None)
    p_read.add_argument("--version", default=None)
    p_read.add_argument("--ecosystem", default=None)
    p_read.set_defaults(func=cmd_read)

    p_mcp = sub.add_parser(
        "serve-mcp",
        help="Start the MCP server (stdio by default)",
        parents=[_config_parent],
    )
    p_mcp.add_argument(
        "--http",
        action="store_true",
        help="Use the Streamable HTTP transport instead (binds loopback only by default)",
    )
    p_mcp.add_argument("--host", default=None, help="HTTP listen address")
    p_mcp.add_argument("--port", type=int, default=None, help="HTTP listen port")
    p_mcp.add_argument("--path", default=None, help="HTTP endpoint path")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    p_web = sub.add_parser(
        "serve-web",
        help="Start the Web server (binds loopback only by default)",
        parents=[_config_parent],
    )
    p_web.add_argument("--host", default=None, help="HTTP listen address")
    p_web.add_argument("--port", type=int, default=None, help="HTTP listen port")
    p_web.set_defaults(func=cmd_serve_web)

    p_repo = sub.add_parser(
        "index-repo",
        help="Index a source repo and persist call instances",
        parents=[_config_parent],
    )
    p_repo.add_argument(
        "library", help="Library name in explicit form, package name in resolved form"
    )
    p_repo.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Required in explicit form; defaults to the stable version in resolved form",
    )
    p_repo.add_argument(
        "source",
        nargs="?",
        default=None,
        help="Remote URL or local path in explicit form; resolved by name by default",
    )
    p_repo.add_argument(
        "--ecosystem",
        default=None,
        help="Resolve by name: pypi|npm|maven|go|crates|nuget; defaults to explicit custom",
    )
    p_repo.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Resolve to the newest version including prereleases",
    )
    p_repo.add_argument(
        "--offline",
        action="store_true",
        help="Read cache only; do not touch the network",
    )
    p_repo.add_argument(
        "--registry-mirror",
        default=None,
        help="Registry mirror preset; defaults to the config value",
    )
    p_repo.add_argument(
        "--update",
        action="store_true",
        help="Pull updates when the repo already exists",
    )
    p_repo.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_repo.set_defaults(func=cmd_index_repo)

    p_mine = sub.add_parser(
        "mine", help="Mine closed call-sequence patterns", parents=[_config_parent]
    )
    p_mine.add_argument("library")
    p_mine.add_argument("version")
    p_mine.add_argument(
        "--ecosystem",
        default=None,
        help="Ecosystem of the library; by default resolved across ecosystems",
    )
    p_mine.add_argument("--minsup", type=int, default=2)
    p_mine.add_argument(
        "--topn",
        type=int,
        default=None,
        help="Explicit pruning: keep only fully qualified names ranked in the top N by call count",
    )
    p_mine.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="Include pending calls in sequences (by default only explicit and inferred ones are used)",
    )
    p_mine.add_argument("--maxlen", type=int, default=10)
    p_mine.add_argument("--minlen", type=int, default=2)
    p_mine.add_argument("--cluster-threshold", type=float, default=0.15)
    p_mine.add_argument("--cluster-min-size", type=int, default=2)
    p_mine.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_mine.set_defaults(func=cmd_mine)

    p_catalog = sub.add_parser(
        "catalog", help="Browse libraries and versions", parents=[_config_parent]
    )
    p_catalog.add_argument("library", nargs="?", default=None)
    p_catalog.add_argument("--ecosystem", default=None)
    p_catalog.set_defaults(func=cmd_catalog)

    p_induce = sub.add_parser(
        "induce-labels",
        help="Induce pattern labels with chat",
        parents=[_config_parent],
    )
    p_induce.add_argument("library")
    p_induce.add_argument("version")
    p_induce.add_argument("--ecosystem", default=None)
    p_induce.add_argument("--topn", type=int, default=None)
    p_induce.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_induce.set_defaults(func=cmd_induce_labels)

    p_api = sub.add_parser(
        "api-insight", help="API usage views", parents=[_config_parent]
    )
    p_api.add_argument("view", choices=["usage", "related", "args", "common"])
    p_api.add_argument("--api", default=None)
    p_api.add_argument("--apis", default=None)
    p_api.add_argument("--library", default=None)
    p_api.add_argument("--version", default=None)
    p_api.add_argument("--ecosystem", default=None)
    p_api.add_argument("--limit", type=int, default=None)
    p_api.set_defaults(func=cmd_api_insight)

    p_pattern = sub.add_parser(
        "pattern-insight", help="Sequence and structure views", parents=[_config_parent]
    )
    p_pattern.add_argument(
        "view",
        choices=[
            "patterns",
            "evidence",
            "tree",
            "clusters",
            "health",
            "labels",
            "snippets",
        ],
    )
    p_pattern.add_argument(
        "--pattern",
        default=None,
        help="Comma-separated sequence for evidence view",
    )
    p_pattern.add_argument(
        "--containing",
        default=None,
        help="Comma-separated APIs that must all be present",
    )
    p_pattern.add_argument("--prefix", default=None)
    p_pattern.add_argument("--library", default=None)
    p_pattern.add_argument("--version", default=None)
    p_pattern.add_argument("--ecosystem", default=None)
    p_pattern.add_argument("--limit", type=int, default=None)
    p_pattern.add_argument("--per-file-limit", type=int, default=None)
    p_pattern.add_argument("--no-dedup", action="store_true")
    p_pattern.set_defaults(func=cmd_pattern_insight)

    p_resolve = sub.add_parser(
        "resolve", help="Resolve a package address", parents=[_config_parent]
    )
    p_resolve.add_argument("name")
    p_resolve.add_argument("--ecosystem", default=None)
    p_resolve.add_argument("--version", default=None)
    p_resolve.add_argument(
        "--target",
        default="docs",
        choices=["docs", "repo"],
    )
    p_resolve.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Resolve to the newest version including prereleases",
    )
    p_resolve.set_defaults(func=cmd_resolve)

    p_dump = sub.add_parser(
        "dump-chunks",
        help="Preview chunking: line ranges, byte offsets, and headings per file",
        parents=[_config_parent],
    )
    group = p_dump.add_mutually_exclusive_group(required=True)
    group.add_argument("--markdown", default=None, help="Markdown document path")
    group.add_argument("--source", default=None, help="Source file path")
    p_dump.add_argument(
        "--url", default=None, help="URL annotation for document chunks"
    )
    p_dump.set_defaults(func=cmd_dump_chunks)

    p_reembed = sub.add_parser(
        "reembed",
        help="Fully re-embed a version's vectors from the source of truth",
        parents=[_config_parent],
    )
    p_reembed.add_argument("library")
    p_reembed.add_argument("version")
    p_reembed.add_argument("--ecosystem", default=None)
    p_reembed.set_defaults(func=cmd_reembed)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="Repair a version's vectors from the source of truth by set difference",
        parents=[_config_parent],
    )
    p_reconcile.add_argument("library")
    p_reconcile.add_argument("version")
    p_reconcile.add_argument("--ecosystem", default=None)
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_fts_rebuild = sub.add_parser(
        "fts-rebuild",
        help="Rebuild the full-text index from the source tables",
        parents=[_config_parent],
    )
    p_fts_rebuild.set_defaults(func=cmd_fts_rebuild)

    p_fts_check = sub.add_parser(
        "fts-check",
        help="Check full-text index row counts and triggers",
        parents=[_config_parent],
    )
    p_fts_check.set_defaults(func=cmd_fts_check)

    p_review = sub.add_parser(
        "review", help="Review pending call instances", parents=[_config_parent]
    )
    review_sub = p_review.add_subparsers(dest="review_action", required=True)
    review_sub.add_parser("list", help="List pending items").add_argument(
        "--limit",
        type=int,
        default=50,
    )
    for _name in ("approve", "conflict", "reopen"):
        _p = review_sub.add_parser(_name)
        _p.add_argument("entity_id", type=int)
    p_review.set_defaults(func=cmd_review)

    p_job = sub.add_parser(
        "job-show", help="Inspect job audit outcomes", parents=[_config_parent]
    )
    p_job.add_argument("job_id", type=int, nargs="?", default=None)
    p_job.add_argument("--list", action="store_true", help="List recent jobs")
    p_job.add_argument("--limit", type=int, default=20)
    p_job.add_argument("--full", action="store_true", help="Print the full outcome")
    p_job.set_defaults(func=cmd_job_show)

    p_config = sub.add_parser(
        "config-show",
        help="Print the effective configuration, redacted",
        parents=[_config_parent],
    )
    p_config.set_defaults(func=cmd_config_show)

    p_purge = sub.add_parser(
        "purge", help="Delete a whole version and log it", parents=[_config_parent]
    )
    p_purge.add_argument("library")
    p_purge.add_argument("version")
    p_purge.add_argument("--ecosystem", default=None)
    p_purge.set_defaults(func=cmd_purge)

    p_del = sub.add_parser(
        "deletions",
        help="View deletion logs / prune expired records",
        parents=[_config_parent],
    )
    p_del.add_argument("--limit", type=int, default=50)
    p_del.add_argument("--full", action="store_true", help="Print full details")
    p_del.add_argument(
        "--prune",
        action="store_true",
        help="Prune expired records per the retention period",
    )
    p_del.add_argument(
        "--ttl-days",
        type=float,
        default=None,
        help="Defaults to the config value deletion_log_ttl_days",
    )
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
