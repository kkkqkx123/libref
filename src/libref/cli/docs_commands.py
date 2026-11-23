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
    as_error,
)
from libref.crawler.registry import RegistryError
from libref.errors import invalid_argument, unavailable
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
            max_pages=getattr(args, "max_pages", None),
            prefilter=bool(getattr(args, "prefilter", False)),
        )
    finally:
        conn.close()
    print(_json.dumps(probe.model_dump(), ensure_ascii=False))
    return 0


def cmd_link_stats(args: argparse.Namespace) -> int:
    """Reference counts over stored document link edges."""
    import json as _json

    from libref.services.docs.links import (
        page_link_stats,
        top_referenced,
        version_link_summary,
    )

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        url = getattr(args, "url", None)
        if url:
            out = page_link_stats(
                conn,
                url,
                args.library,
                args.version,
                getattr(args, "ecosystem", None),
            )
        else:
            out = {
                "summary": version_link_summary(
                    conn,
                    args.library,
                    args.version,
                    getattr(args, "ecosystem", None),
                ),
                "items": top_referenced(
                    conn,
                    args.library,
                    args.version,
                    getattr(args, "ecosystem", None),
                    limit=getattr(args, "limit", 20),
                ),
            }
    except ValueError as exc:
        raise as_error(exc) from exc
    finally:
        conn.close()
    print(_json.dumps(out, ensure_ascii=False))
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


def _remote_ingest(args: argparse.Namespace, settings) -> int:
    import time as _time

    import httpx as _httpx3

    from libref.cli.remote import auth_headers as _ah3
    from libref.cli.remote import base_url_for as _buf3

    base = _buf3(args, settings)
    # Resolve mode not supported remotely; explicit form only
    if (
        getattr(args, "ecosystem", None) is not None
        or getattr(args, "start_url", None) is None
    ):
        raise invalid_argument(
            "remote ingest requires explicit library version start_url "
            "(resolve locally first)",
            field="start_url",
        )
    payload = {
        "library": args.library,
        "version": args.version,
        "start_url": args.start_url,
        "ecosystem": "custom",
    }
    if getattr(args, "max_pages", None) is not None:
        payload["max_pages"] = int(args.max_pages)
    if getattr(args, "max_pages", None) is not None:
        # server caps via max_pages query? ScrapeRequest has no max_pages? use jobs path? send as is
        pass
    try:
        with _httpx3.Client(timeout=30.0) as _c:
            _r = _c.post(f"{base}/scrape", json=payload, headers=_ah3(settings))
            _r.raise_for_status()
            data = _r.json()
    except Exception as exc:
        raise as_error(exc) from exc
    job_id = data.get("id")
    print(f"job {job_id}")
    # Poll until settle, reusing wait semantics
    deadline = _time.monotonic() + 300.0
    while True:
        try:
            with _httpx3.Client(timeout=30.0) as _c2:
                _q = _c2.get(f"{base}/jobs/{int(job_id)}", headers=_ah3(settings))
                _q.raise_for_status()
                info = _q.json()
        except Exception as exc:
            raise unavailable(f"remote poll failed: {exc}") from exc
        status = info.get("status")
        print(
            f"job {info.get('id')} {status}"
            + (f" error={info.get('error')}" if info.get("error") else "")
        )
        if status in ("done", "failed"):
            return 0 if status == "done" else 1
        if _time.monotonic() >= deadline:
            raise unavailable("remote poll timeout")
        _time.sleep(1.0)


def cmd_ingest(args: argparse.Namespace) -> int:
    settings = _load_settings(args, _registry_overrides(args))
    from libref.cli.remote import is_remote as _is_remote3

    if _is_remote3(args, settings):
        return _remote_ingest(args, settings)
    conn = _connect(settings)
    vec_store, embed = _build_vector(settings)
    scope = _scope_from_args(args)
    try:
        resolve_mode = args.ecosystem is not None or args.start_url is None
        if resolve_mode:
            if not args.library:
                raise invalid_argument(
                    "resolve mode requires library (package name)", field="library"
                )
            if args.start_url is not None:
                raise invalid_argument(
                    "--ecosystem and start_url are mutually exclusive",
                    field="ecosystem",
                )
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
                raise as_error(exc) from exc
            except RegistryError as exc:
                raise as_error(exc) from exc
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
                raise invalid_argument(
                    "explicit mode requires library version start_url"
                )
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


def _remote_search(args: argparse.Namespace, settings) -> int:
    from libref.cli.remote import base_url_for

    base = base_url_for(args, settings)
    params = {
        "query": args.query,
        "limit": int(args.limit),
        "preset": getattr(args, "preset", "balanced"),
    }
    for _k in ("library", "version", "ecosystem"):
        _v = getattr(args, _k, None)
        if _v is not None:
            params[_k] = _v
    if getattr(args, "suggest", False):
        params["suggest"] = "true"
    if getattr(args, "include_prerelease", False):
        params["include_prerelease"] = "true"
    import httpx as _httpx

    from libref.cli.remote import auth_headers as _ah

    try:
        with _httpx.Client(timeout=30.0) as _c:
            _r = _c.get(f"{base}/search", params=params, headers=_ah(settings))
            _r.raise_for_status()
            data = _r.json()
    except Exception as exc:
        raise as_error(exc) from exc
    for r in data.get("items") or []:
        print(
            f"[{r.get('score', 0):.3f}] {r.get('title') or '(untitled)'} — {r.get('url')}"
        )
        print(f"    {r.get('snippet')}")
    if data.get("degraded"):
        print(
            f"(degraded: {','.join(data.get('reasons', []) or [])})",
            file=__import__("sys").stderr,
        )
    if not (data.get("items") or []):
        print("(no results)")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import json as _json

    settings = _load_settings(args, _window_overrides(args))
    from libref.cli.remote import is_remote as _is_remote

    if _is_remote(args, settings):
        return _remote_search(args, settings)
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
                block_type=getattr(args, "block_type", None),
                exclude_deprecated=bool(getattr(args, "exclude_deprecated", False)),
                aligned_only=bool(getattr(args, "aligned_only", False)),
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
            block_type=getattr(args, "block_type", None),
            exclude_deprecated=bool(getattr(args, "exclude_deprecated", False)),
            aligned_only=bool(getattr(args, "aligned_only", False)),
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


def cmd_doc_insight(args: argparse.Namespace) -> int:
    import json as _json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        from libref.services.docs.doc_insight import DocInsightService

        rows = DocInsightService(conn, settings).insight(
            args.library,
            args.version,
            view=args.view,
            ecosystem=getattr(args, "ecosystem", None),
            profile=getattr(args, "profile", None),
            facet=getattr(args, "facet", None),
            api=getattr(args, "api", None),
            pattern=getattr(args, "pattern", None),
            verdict=getattr(args, "verdict", None),
            block_type=getattr(args, "block_type", None),
            limit=int(args.limit),
        )
        print(_json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


def cmd_doc_features(args: argparse.Namespace) -> int:
    import json as _json

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        if getattr(args, "run_async", False):
            from libref.services.docs.feature_jobs import (
                run_doc_feature_job_sync,
                start_doc_feature_job,
            )

            mgr = JobManager(conn, stale_after=float(settings.heartbeat_timeout))
            mgr.recover()
            job_id = start_doc_feature_job(
                mgr,
                args.library,
                args.version,
                ecosystem=getattr(args, "ecosystem", None),
                profile=getattr(args, "profile", None),
                scope=args.scope,
            )
            job = run_doc_feature_job_sync(mgr, settings, conn, job_id)
            _print_job(job)
            return 0 if job.status == "done" else 1

        from libref.services.docs.feature_refresh import refresh_doc_features

        counts = refresh_doc_features(
            conn,
            args.library,
            args.version,
            ecosystem=getattr(args, "ecosystem", None),
            profile=getattr(args, "profile", None),
            scope=args.scope,
            settings=settings,
        )
        print(_json.dumps(counts, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def _remote_read(args: argparse.Namespace, settings) -> int:
    import json as _json

    import httpx as _httpx2

    from libref.cli.remote import auth_headers as _ah2
    from libref.cli.remote import base_url_for as _buf

    base = _buf(args, settings)
    params = {
        "url": args.url,
        "mode": args.mode,
        "radius": int(getattr(args, "radius", 1) or 1),
    }
    for _k in ("sort_order", "source_group_id", "library", "version", "ecosystem"):
        _v = getattr(args, _k, None)
        if _v is not None:
            params[_k] = _v
    try:
        with _httpx2.Client(timeout=30.0) as _c:
            _r = _c.get(f"{base}/read", params=params, headers=_ah2(settings))
            if _r.status_code == 404:
                print(f"(not found: {args.url})", file=__import__("sys").stderr)
                return 1
            _r.raise_for_status()
            page = _r.json()
    except Exception as exc:
        raise as_error(exc) from exc
    if args.mode == "page":
        print(page.get("content", ""))
    else:
        print(_json.dumps(page, ensure_ascii=False))
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    import json as _json

    _st = _load_settings(args)
    from libref.cli.remote import is_remote as _is_remote2

    if _is_remote2(args, _st):
        return _remote_read(args, _st)
    conn = _connect(_st)
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
            raise as_error(exc) from exc
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
        raise invalid_argument(f"unsupported suffix: {suffix}", field="source")
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
