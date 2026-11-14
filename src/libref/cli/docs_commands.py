"""Document commands: ingest, refresh, search, answer, read."""

from __future__ import annotations

import argparse
import sys

from libref.cli.common import (
    _build_vector,
    _connect,
    _load_settings,
    _print_job,
    _print_suggestions,
    _registry_overrides,
    _resolve_for_cli,
    _scope_from_args,
    _window_overrides,
)
from libref.crawler.registry import RegistryError
from libref.services.docs.answer import answer_query
from libref.services.docs.ingest import (
    IngestService,
    run_scrape_job_sync,
    start_refresh_job,
    start_scrape_job,
)
from libref.services.docs.pages import PageService
from libref.services.factory import build_ingest_service, build_search_service
from libref.services.infra.jobs import JobManager


def cmd_refresh(args: argparse.Namespace) -> int:
    """Refresh-intent entry: reuses the scrape path's incremental semantics."""
    settings = _load_settings(args)
    conn = _connect(settings)
    vec_store, embed = _build_vector(settings)
    scope = _scope_from_args(args)
    try:
        if getattr(args, "run_async", False):
            mgr = JobManager(conn, stale_after=float(settings.heartbeat_timeout))
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
                fresh=bool(getattr(args, "fresh", False)),
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
            fresh=bool(getattr(args, "fresh", False)),
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_probe_docs(args: argparse.Namespace) -> int:
    """Read-only documentation probe: discovery shape without storing."""
    import json as _json

    from libref.services.docs.probe import probe_docs

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        probe = probe_docs(
            conn,
            settings,
            args.start_url,
            scope=_scope_from_args(args),
            sample_limit=getattr(args, "sample_limit", None),
            fetch_sample_limit=getattr(args, "fetch_sample_limit", None),
        )
    finally:
        conn.close()
    print(_json.dumps(probe.model_dump(), ensure_ascii=False))
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
                from libref.cli.common import format_registry_error

                print(
                    format_registry_error(exc, target="docs"),
                    file=sys.stderr,
                )
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
            mgr = JobManager(conn, stale_after=float(settings.heartbeat_timeout))
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
                fresh=bool(getattr(args, "fresh", False)),
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
            fresh=bool(getattr(args, "fresh", False)),
        )
    finally:
        conn.close()
    for outcome, n in counts.items():
        print(f"{outcome}: {n}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import json as _json

    settings = _load_settings(args, _window_overrides(args))
    conn = _connect(settings)
    try:
        service, _gw = build_search_service(conn, settings)
        preset = getattr(args, "preset", "balanced")
        results, status = _asyncio.run(
            service.search_with_mode(
                args.query,
                preset,
                library=args.library,
                version=args.version,
                ecosystem=getattr(args, "ecosystem", None),
                limit=args.limit,
            )
        )
        degraded = bool(status.get("degraded", False))
        reasons = list(status.get("reasons", []) or [])
        if degraded:
            suffix = f": {','.join(reasons)}" if reasons else ""
            print(f"(degraded: partial retrieval{suffix})", file=sys.stderr)
        if status["evidence"] == "none" and results:
            print(
                "(weak evidence: the retrieved materials do not cover this query)",
                file=sys.stderr,
            )
        if status["cross_lingual"]:
            print(
                "(cross-lingual query: full-text lane matched nothing; "
                "results come from the vector lane)",
                file=sys.stderr,
            )
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
            rerank=gateway.rerank_fn(),
            chat=gateway.chat_fn(),
            preset=getattr(args, "preset", "balanced"),
        )
    finally:
        conn.close()
    if not out["answer"]:
        if "low_evidence" in (out.get("reasons") or []):
            print("(no answer: the retrieved materials do not cover this query)")
        else:
            print("(no evidence)")
        return 0
    print(out["answer"])
    for c in out["citations"]:
        print(f"- {c['title'] or '(untitled)'} — {c['url']}")
    if out.get("degraded"):
        reasons = list(out.get("reasons", []) or [])
        suffix = f": {','.join(reasons)}" if reasons else ""
        print(f"(degraded: partial retrieval{suffix})", file=sys.stderr)
    if out.get("confidence") == "low":
        print("(low confidence: answer cites no material)", file=sys.stderr)
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
