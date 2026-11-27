"""Shared runtime for the offline performance benchmark.

The harness owns timing, statistics, environment capture, report writing and
baseline comparison. Cases call production entry points directly; no benchmark
specific index or retrieval implementation lives here.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import random
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUTPUTS = HERE / "outputs"
DEFAULT_DATASET = ROOT / "tests/e2e/bench/data/bench.db"
JUDGMENTS = ROOT / "tests/e2e/bench/judgments.json"
FIXTURE_ROOT = ROOT / "tests/fixtures/parsing"

_GIT_COMMIT = "unavailable"


def load_spec(path: Path | None = None) -> dict[str, Any]:
    """Read the versioned case specification."""
    source = path or HERE / "perf.json"
    return json.loads(source.read_text(encoding="utf-8"))


def environment() -> dict[str, Any]:
    """Capture benign machine facts that decide whether two runs are comparable."""
    cpu = platform.processor() or "unknown"
    try:
        lines = Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines()
        cpu = next(
            line.split(":", 1)[1].strip()
            for line in lines
            if line.startswith("model name")
        )
    except OSError, StopIteration:
        pass
    commit = _git_commit()
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": f"{platform.system()} {platform.release()} {platform.machine()}",
        "cpu": cpu,
        "logical_cores": os.cpu_count() or 0,
        "commit": commit,
    }


def _git_commit() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return "unavailable"
    return proc.stdout.strip() or "unavailable"


def settings_snapshot(settings: Any) -> dict[str, Any]:
    """Redacted view of the knobs that can change benchmark numbers."""
    from libref.config import redacted_dict

    full = redacted_dict(settings)
    keys = (
        "db_path",
        "vector_backend",
        "embedding_dim",
        "rerank_enabled",
        "rerank_candidate_limit",
        "rerank_top_n",
        "chat_enabled",
        "chunk_embed_max_tokens",
        "chunk_embed_overlap_tokens",
        "chunk_embed_min_tokens",
        "chunk_reserve_ratio",
        "index_batch_files",
        "ingest_batch_pages",
        "rrf_k",
        "candidate_multiplier",
        "assemble_multiplier",
        "assemble_min",
        "minsup",
        "maxlen",
        "minlen",
        "graph_max_depth",
        "graph_max_nodes",
        "graph_max_fanout",
        "graph_pattern_limit",
        "graph_query_budget",
        "graph_cache_enabled",
        "graph_cache_max_edges",
        "graph_cache_ttl_seconds",
        "auth_mode",
    )
    return {key: full[key] for key in keys if key in full}


def stats(values: list[float]) -> dict[str, float]:
    """Nearest-rank percentiles plus dispersion over wall-clock milliseconds."""
    if not values:
        return {
            "min_ms": 0.0,
            "median_ms": 0.0,
            "p95_ms": 0.0,
            "max_ms": 0.0,
            "cv": 0.0,
        }
    ordered = sorted(values)
    mean = statistics.fmean(ordered)
    cv = statistics.pstdev(ordered) / mean if mean else 0.0
    return {
        "min_ms": round(ordered[0], 4),
        "median_ms": round(statistics.median(ordered), 4),
        "p95_ms": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 4),
        "max_ms": round(ordered[-1], 4),
        "cv": round(cv, 6),
    }


def measure(
    fn: Callable[[], Any],
    *,
    warmup: int,
    repeats: int,
    trace_alloc: bool = False,
) -> dict[str, Any]:
    """Measure one callable, keeping tracing out of the timed region only where possible."""
    for _ in range(int(warmup)):
        fn()
    samples: list[float] = []
    cpu: list[float] = []
    peaks: list[int] = []
    value: Any = None
    for _ in range(int(repeats)):
        if trace_alloc:
            tracemalloc.start()
        started = time.perf_counter()
        cpu_started = time.process_time()
        value = fn()
        cpu.append((time.process_time() - cpu_started) * 1000)
        samples.append((time.perf_counter() - started) * 1000)
        if trace_alloc:
            peaks.append(tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
    measured = {
        "samples_ms": [round(item, 4) for item in samples],
        "stats": stats(samples),
        "cpu_median_ms": round(statistics.median(cpu), 4) if cpu else 0.0,
    }
    if trace_alloc:
        measured["peak_traced_bytes"] = max(peaks) if peaks else 0
    return {"measured": measured, "value": value}


def measure_each(
    fn: Callable[[Any], Any],
    items: list[Any],
    *,
    warmup: int,
    repeats: int,
) -> dict[str, Any]:
    """Measure a per-query workload and return one sample per invocation."""
    for _ in range(int(warmup)):
        for item in items:
            fn(item)
    samples: list[float] = []
    for _ in range(int(repeats)):
        for item in items:
            started = time.perf_counter()
            fn(item)
            samples.append((time.perf_counter() - started) * 1000)
    return {"samples_ms": [round(item, 4) for item in samples], "stats": stats(samples)}


def _result(
    case_id: str,
    variant: str,
    measured: dict[str, Any],
    *,
    counters: dict[str, Any] | None = None,
    failures: list[str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "case_id": case_id,
        "variant": variant,
        **measured,
    }
    if counters:
        item["counters"] = counters
    if failures:
        item["failures"] = failures
    if notes:
        item["notes"] = notes
    return item


def _load_queries(fixture: str, limit: int) -> list[str]:
    rows = json.loads(JUDGMENTS.read_text(encoding="utf-8")).get(fixture, [])
    out: list[str] = []
    for row in rows:
        if row.get("type") == "absent":
            continue
        query = str(row.get("query") or "").strip()
        if query:
            out.append(query)
        if len(out) >= limit:
            break
    if not out:
        raise RuntimeError(f"no benchmark queries for {fixture}")
    return out


def _manifest(dataset: Path) -> dict[str, Any]:
    path = dataset.parent / "manifest.json"
    if not path.is_file():
        raise RuntimeError(f"dataset manifest missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_for(ctx: dict[str, Any], dataset: Path) -> dict[str, Any]:
    return ctx.get("manifest") or _manifest(dataset)


def _connect(dataset: Path, *, threaded: bool = False) -> sqlite3.Connection:
    from libref.db import open_db

    return open_db(dataset, check_same_thread=threaded)


def _bench_settings(dataset: Path, **overrides: Any) -> Any:
    from tests.e2e.bench.fakes import bench_settings

    return bench_settings(dataset, **overrides)


def _scope(info: dict[str, Any]) -> dict[str, str]:
    return {
        "library": str(info["library"]),
        "version": str(info["version"]),
        "ecosystem": str(info["ecosystem"]),
    }


def case_prefixspan(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    from libref.mining import PrefixSpan

    out: list[dict[str, Any]] = []
    for index, size in enumerate(cfg["sizes"]):
        alphabet = int(cfg["alphabet"][index])
        length = int(cfg["sequence_length"])
        rng = random.Random(int(cfg["seed"]) + int(size))
        db = [
            [
                rng.randrange(alphabet)
                for _ in range(rng.randint(length - 3, length + 3))
            ]
            for _ in range(int(size))
        ]

        def run(*, db=db, cfg=cfg) -> Any:
            miner = PrefixSpan(db)
            miner.minlen = int(cfg["minlen"])
            miner.maxlen = int(cfg["maxlen"])
            return miner.frequent(int(cfg["minsup"]), closed=True)

        measured = measure(
            run,
            warmup=cfg["warmup"],
            repeats=cfg["repeats"],
            trace_alloc=True,
        )
        patterns = measured["value"] or []
        failures = [] if patterns else ["prefixspan returned no bounded patterns"]
        out.append(
            _result(
                "micro.prefixspan",
                f"n={size}",
                measured["measured"],
                counters={
                    "sequences": int(size),
                    "alphabet": alphabet,
                    "patterns": len(patterns),
                },
                failures=failures,
            )
        )
    return out


def case_chunking(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    from libref.chunking import split_markdown_dual

    base = (ROOT / "tests/fixtures/docs/sample.md").read_text(encoding="utf-8")
    out: list[dict[str, Any]] = []
    for copies in cfg["copies"]:
        text = "\n\n".join(f"# Section {idx}\n\n{base}" for idx in range(int(copies)))

        def run(*, text=text) -> Any:
            return split_markdown_dual(text, "https://docs.example.com/bench")

        measured = measure(
            run, warmup=cfg["warmup"], repeats=cfg["repeats"], trace_alloc=True
        )
        dual = measured["value"]
        chunks = dual.embedding + dual.bm25
        unbalanced = sum(1 for chunk in chunks if chunk.content.count("```") % 2)
        failures = [] if chunks and not unbalanced else ["chunk fence invariant failed"]
        out.append(
            _result(
                "micro.chunking",
                f"copies={copies}",
                measured["measured"],
                counters={
                    "embedding_chunks": len(dual.embedding),
                    "bm25_chunks": len(dual.bm25),
                    "empty_dropped": dual.empty_dropped,
                    "unbalanced_fences": unbalanced,
                },
                failures=failures,
            )
        )
    return out


def case_ast(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine

    out: list[dict[str, Any]] = []
    for language in cfg["languages"]:
        config = ast_config.LANGUAGE_CONFIGS.get(str(language))
        suffix = config.file_suffixes[0] if config is not None else ""
        fallback = {
            "python": "python_async_decorator.py",
            "javascript": "js_chain_import.js",
        }
        source_path = FIXTURE_ROOT / fallback.get(
            str(language), f"{language}_shapes{suffix}"
        )
        if config is None or not source_path.is_file():
            out.append(
                _result(
                    "micro.ast",
                    str(language),
                    {"samples_ms": [], "stats": stats([])},
                    notes=["fixture unavailable: case skipped"],
                )
            )
            continue
        source = source_path.read_bytes()

        def run(*, suffix=suffix, config=config, source=source) -> Any:
            return ast_engine.extract_file(f"bench{suffix}", config, source)

        warm = measure(run, warmup=1, repeats=int(cfg["repeats"]))
        cold_samples: list[float] = []
        cold_value: Any = None
        for _ in range(int(cfg["repeats"])):
            from libref.astminer import parse as ast_parse

            ast_parse._parsers.clear()
            ast_parse._compiled.clear()
            started = time.perf_counter()
            cold_value = run()
            cold_samples.append((time.perf_counter() - started) * 1000)
        decls, aliases, calls = cold_value
        out.append(
            _result(
                "micro.ast",
                f"{language}/warm",
                warm["measured"],
                counters={
                    "declarations": len(decls),
                    "aliases": len(aliases),
                    "calls": len(calls),
                },
            )
        )
        out.append(
            _result(
                "micro.ast",
                f"{language}/cold",
                {
                    "samples_ms": [round(v, 4) for v in cold_samples],
                    "stats": stats(cold_samples),
                },
            )
        )
    return out


def case_pipeline_index(
    cfg: dict[str, Any], ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    from tests.e2e.bench.build import build

    names = [str(name) for name in cfg["fixtures"]]
    stage_names = (
        "clean",
        "split",
        "store",
        "commit",
        "unpack",
        "index",
        "verify",
        "mine",
    )
    stage_samples: dict[str, list[float]] = {name: [] for name in stage_names}
    total_samples: list[float] = []
    manifest: dict[str, Any] = {}
    dataset = OUTPUTS / "dataset"
    if dataset.exists():
        shutil.rmtree(dataset)
    for repeat in range(int(cfg["repeats"])):
        target = (
            dataset
            if repeat == 0
            else Path(tempfile.mkdtemp(prefix="libref-perf-build-"))
        )
        try:
            manifest = build(target, names=names)
            total = sum(
                int(info.get("elapsed_ms", 0)) for info in manifest["fixtures"].values()
            )
            total_samples.append(float(total))
            for info in manifest["fixtures"].values():
                for name, value in (info.get("stages_ms") or {}).items():
                    stage_samples.setdefault(name, []).append(float(value))
        finally:
            if repeat:
                shutil.rmtree(target, ignore_errors=True)
    ctx["dataset"] = dataset / "bench.db"
    ctx["manifest"] = manifest
    out = [
        _result(
            "pipeline.index",
            "total",
            {
                "samples_ms": [round(v, 4) for v in total_samples],
                "stats": stats(total_samples),
            },
            counters={"fixtures": len(names)},
        )
    ]
    for name, values in stage_samples.items():
        if not values:
            continue
        item = _result(
            "pipeline.index",
            f"stage={name}",
            {"samples_ms": [round(v, 4) for v in values], "stats": stats(values)},
        )
        if stats(values)["median_ms"] == 0:
            item["notes"] = ["sub-millisecond stage at integer clock resolution"]
        out.append(item)
    return out


def case_fts_scope(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    from libref.config import setting_or_default
    from libref.store.retriever import fts_search

    dataset = ctx.get("dataset", DEFAULT_DATASET)
    fixture = str(cfg["fixture"])
    info = _manifest_for(ctx, dataset)["fixtures"][fixture]
    queries = _load_queries(fixture, int(cfg["queries"]))
    settings = _bench_settings(dataset)
    weights = tuple(
        float(setting_or_default(settings, key))
        for key in ("fts_w_content", "fts_w_title", "fts_w_url", "fts_w_path")
    )
    conn = _connect(dataset)
    try:
        out: list[dict[str, Any]] = []
        for scope in ("single", "library", "global"):
            scoped = (
                {
                    "library": info["library"],
                    "version": info["version"],
                    "ecosystem": info["ecosystem"],
                }
                if scope in ("single", "library")
                else {}
            )
            if scope == "library":
                scoped.pop("version")

            def run(query: str, *, scoped=scoped, weights=weights, cfg=cfg) -> Any:
                return fts_search(
                    conn,
                    query,
                    **scoped,
                    weights=weights,
                    budget=int(cfg["budget"]),
                )

            measured = measure_each(
                run, queries, warmup=int(cfg["warmup"]), repeats=int(cfg["repeats"])
            )
            hits = len(run(queries[0]))
            out.append(
                _result(
                    "store.fts_scope",
                    scope,
                    measured,
                    counters={"queries": len(queries), "first_query_hits": hits},
                )
            )

        captured: list[str] = []
        scoped = _scope(info)
        conn.set_trace_callback(captured.append)
        fts_search(
            conn,
            queries[0],
            **scoped,
            weights=weights,
            budget=int(cfg["budget"]),
        )
        conn.set_trace_callback(None)
        sql = next((item for item in captured if "documents_fts MATCH" in item), "")
        plan: list[str] = []
        if sql.lower().startswith("select"):
            try:
                plan = [
                    str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
                ]
            except sqlite3.Error:
                plan = []
        indexed = "EXISTS (SELECT 1 FROM documents" in sql
        out.append(
            _result(
                "store.fts_scope",
                "audit",
                {"samples_ms": [], "stats": stats([])},
                counters={
                    "indexed_scope_probe": int(indexed),
                    "plan_rows": len(plan),
                    "plan_detail": " | ".join(plan)[:300],
                },
                failures=[] if indexed else ["scoped FTS lost the indexed probe"],
                notes=["audit-only observation; no timing samples"],
            )
        )
        return out
    finally:
        conn.close()


def case_html_clean(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Attribute the document cleaning stage to its individual pure steps."""
    import gzip
    import hashlib

    from markdownify import markdownify as md
    from selectolax.lexbor import LexborHTMLParser

    from libref.crawler import extract
    from libref.text.normalize import normalize_markdown

    path = ROOT / "tests/e2e/bench/fixtures" / str(cfg["fixture"]) / "pages.jsonl.gz"
    pages: list[tuple[str, str]] = []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            pages.append((str(row.get("body") or ""), str(row.get("url") or "")))
            if len(pages) >= int(cfg["pages"]):
                break
    if not pages:
        raise RuntimeError(f"no frozen pages for {cfg['fixture']}")

    def tree_of(raw: str) -> LexborHTMLParser:
        return LexborHTMLParser(raw)

    def strip_noise(tree: LexborHTMLParser) -> None:
        for tag in extract._STRIP_TAGS:
            for node in list(tree.css(tag)):
                node.decompose()
        for node in list(tree.css('[role="navigation"]')):
            node.decompose()

    def root_of(raw: str, url: str, *, prune: bool = False, empty: bool = False):
        tree = tree_of(raw)
        _ = extract._extract_metadata(tree, url)
        if url:
            extract._absolutize_links(tree, url)
        _ = extract._collect_structure_links(tree, url)
        strip_noise(tree)
        root = extract._pick_main_root(tree)
        if prune:
            extract._prune_scored(
                root,
                threshold=extract._FIXED_THRESHOLD,
                threshold_type="fixed",
                min_word_threshold=None,
                preserve_tags=set(),
                preserve_classes=set(),
            )
        if empty:
            extract._prune_empty_nodes(root)
        return root

    stages: dict[str, tuple[Any, Any]] = {
        "parser": (lambda raw, url: raw, lambda state: tree_of(state[0])),
        "metadata": (
            lambda raw, url: (tree_of(raw), url),
            lambda state: extract._extract_metadata(state[0], state[1]),
        ),
        "absolutize": (
            lambda raw, url: (tree_of(raw), url),
            lambda state: extract._absolutize_links(state[0], state[1]),
        ),
        "structure_links": (
            lambda raw, url: (tree_of(raw), url),
            lambda state: extract._collect_structure_links(state[0], state[1]),
        ),
        "noise_strip": (
            lambda raw, url: tree_of(raw),
            lambda state: strip_noise(state),
        ),
        "score_prune": (
            lambda raw, url: root_of(raw, url),
            lambda state: extract._prune_scored(
                state,
                threshold=extract._FIXED_THRESHOLD,
                threshold_type="fixed",
                min_word_threshold=None,
                preserve_tags=set(),
                preserve_classes=set(),
            ),
        ),
        "empty_prune": (
            lambda raw, url: root_of(raw, url, prune=True),
            lambda state: extract._prune_empty_nodes(state),
        ),
        "content_links": (
            lambda raw, url: (root_of(raw, url, prune=True, empty=True), url),
            lambda state: extract._collect_links(state[0], state[1]),
        ),
        "serialize": (
            lambda raw, url: root_of(raw, url, prune=True, empty=True),
            lambda state: state.html or "",
        ),
        "markdown": (
            lambda raw, url: root_of(raw, url, prune=True, empty=True).html or "",
            lambda state: md(state, heading_style="ATX", strip=["img"]),
        ),
        "normalize": (
            lambda raw, url: md(
                root_of(raw, url, prune=True, empty=True).html or "",
                heading_style="ATX",
                strip=["img"],
            ),
            lambda state: normalize_markdown(state),
        ),
        "cache_key": (
            lambda raw, url: raw,
            lambda state: hashlib.sha1(
                f"html\0fingerprint\0{state}".encode("utf-8", "ignore")
            ).hexdigest(),
        ),
    }

    def run_stage(action: Any, setup: Any) -> float:
        elapsed = 0.0
        for raw, url in pages:
            state = setup(raw, url)
            started = time.perf_counter()
            action(state)
            elapsed += (time.perf_counter() - started) * 1000
        return elapsed

    out: list[dict[str, Any]] = []
    for name, (setup, action) in stages.items():
        for _ in range(int(cfg["warmup"])):
            run_stage(action, setup)
        samples = [run_stage(action, setup) for _ in range(int(cfg["repeats"]))]
        out.append(
            _result(
                "micro.html_clean",
                name,
                {"samples_ms": samples, "stats": stats(samples)},
                counters={
                    "pages": len(pages),
                    "bytes": sum(len(raw) for raw, _ in pages),
                },
            )
        )
    return out


def case_mining_scan(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    dataset = ctx.get("dataset", DEFAULT_DATASET)
    fixture = str(cfg["fixture"])
    info = _manifest_for(ctx, dataset)["fixtures"][fixture]
    scope = _scope(info)
    conn = _connect(dataset)
    try:
        from libref.services.code.mining_refresh import mine_patterns

        def run() -> Any:
            return mine_patterns(
                conn,
                scope["library"],
                scope["version"],
                ecosystem=scope["ecosystem"],
                profile="implementation",
            )

        measured = measure(run, warmup=0, repeats=int(cfg["repeats"]))
        row = conn.execute(
            "SELECT count(*) FROM call_instances WHERE in_target_api=1"
        ).fetchone()
        calls = int(row[0]) if row else 0
        captured: list[str] = []
        conn.set_trace_callback(captured.append)
        run()
        conn.set_trace_callback(None)
        scans = sum(
            1
            for item in captured
            if item.startswith("SELECT callee_name, caller_file, caller, confidence")
        )
        counts = {
            table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("seq_patterns", "feature_nodes", "api_clusters")
        }
        failures = [] if scans == 1 else [f"expected one call scan, got {scans}"]
        failures.extend(
            f"mining derived table {table} is empty"
            for table, value in counts.items()
            if not value
        )
        return [
            _result(
                "store.mining_scan",
                "mine_patterns",
                measured["measured"],
                counters={
                    "target_calls": calls,
                    "call_instances_scans": scans,
                    **counts,
                },
                failures=failures,
            )
        ]
    finally:
        conn.close()


def case_service_retrieval(
    cfg: dict[str, Any], ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    dataset = ctx.get("dataset", DEFAULT_DATASET)
    fixture = str(cfg["fixture"])
    info = _manifest_for(ctx, dataset)["fixtures"][fixture]
    scope = _scope(info)
    queries = _load_queries(fixture, int(cfg["queries"]))
    settings = _bench_settings(dataset)
    conn = _connect(dataset)
    try:
        from libref.services.docs.search import SearchService
        from tests.e2e.bench.fakes import (
            BagOfTermsEmbedder,
            SqliteVecStore,
            lexical_rerank,
        )

        embedder = BagOfTermsEmbedder()
        vectors = SqliteVecStore(conn, embedder)
        asyncio.run(vectors.prepare())
        services: dict[str, tuple[Any, str]] = {
            "precise": (SearchService(conn, settings), "precise"),
            "fused": (
                SearchService(conn, settings, vec_store=vectors, embed=embedder),
                "balanced",
            ),
            "fused_rerank": (
                SearchService(
                    conn,
                    settings,
                    vec_store=vectors,
                    embed=embedder,
                    rerank=lexical_rerank(),
                ),
                "balanced",
            ),
        }
        out: list[dict[str, Any]] = []
        for name, (service, preset) in services.items():

            async def one(
                query: str, *, service=service, preset=preset, scope=scope
            ) -> Any:
                return await service.search_with_mode(query, preset, **scope, limit=8)

            measured = measure_each(
                lambda query: asyncio.run(one(query)),
                queries,
                warmup=int(cfg["warmup"]),
                repeats=int(cfg["repeats"]),
            )
            rows, _status = asyncio.run(one(queries[0]))
            out.append(
                _result(
                    "service.retrieval",
                    name,
                    measured,
                    counters={
                        "queries": len(queries),
                        "first_query_results": len(rows),
                    },
                )
            )
        return out
    finally:
        conn.close()


def case_exit_web(cfg: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    dataset = ctx.get("dataset", DEFAULT_DATASET)
    fixture = str(cfg["fixture"])
    info = _manifest_for(ctx, dataset)["fixtures"][fixture]
    scope = _scope(info)
    queries = _load_queries(fixture, int(cfg["queries"]))
    settings = _bench_settings(dataset, auth_mode="dev")
    conn = _connect(dataset, threaded=True)
    try:
        import httpx

        from libref.web import build_web_app

        app = build_web_app(conn, settings)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://perf"
        )

        async def batch(concurrency: int) -> int:
            semaphore = asyncio.Semaphore(concurrency)

            async def one(query: str) -> int:
                async with semaphore:
                    response = await client.get(
                        "/search",
                        params={"query": query, "mode": "precise", **scope, "limit": 8},
                    )
                if response.status_code != 200:
                    raise RuntimeError(f"/search returned {response.status_code}")
                return int(response.json().get("count", 0))

            counts = await asyncio.gather(*(one(query) for query in queries))
            return sum(counts)

        out: list[dict[str, Any]] = []
        for concurrency in cfg["concurrency"]:
            samples: list[float] = []
            for _ in range(int(cfg["warmup"])):
                asyncio.run(batch(int(concurrency)))
            hits = 0
            for _ in range(int(cfg["repeats"])):
                started = time.perf_counter()
                hits = asyncio.run(batch(int(concurrency)))
                samples.append((time.perf_counter() - started) * 1000)
            out.append(
                _result(
                    "exit.web",
                    f"concurrency={concurrency}",
                    {
                        "samples_ms": [round(item, 4) for item in samples],
                        "stats": stats(samples),
                    },
                    counters={
                        "requests_per_batch": len(queries),
                        "result_rows": hits,
                    },
                )
            )
        asyncio.run(client.aclose())
        return out
    finally:
        conn.close()


def case_graph_adjacency(
    cfg: dict[str, Any], ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    """Compare SQL adjacency reads with cold and warm derived snapshots."""
    from libref.services.graph_query import GraphQueryService
    from libref.services.registry.libraries import mining_version_ids
    from libref.store.graph_cache import (
        clear_graph_adjacency_caches,
        get_graph_adjacency_cache,
    )

    dataset = ctx.get("dataset", DEFAULT_DATASET)
    fixture = str(cfg["fixture"])
    variants = ("sql", "cache_cold", "cache_warm")
    max_edges = int(cfg["graph_cache_max_edges"])
    ttl = float(cfg["graph_cache_ttl_seconds"])
    cache = get_graph_adjacency_cache(max_edges, ttl)

    def skipped(reason: str) -> list[dict[str, Any]]:
        return [
            _result(
                "service.graph_adjacency",
                name,
                {"samples_ms": [], "stats": stats([])},
                notes=[f"skipped: {reason}"],
            )
            for name in variants
        ]

    if not dataset.is_file() or not (dataset.parent / "manifest.json").is_file():
        return skipped("benchmark dataset unavailable")
    ctx["dataset"] = dataset
    info = _manifest_for(ctx, dataset)["fixtures"].get(fixture)
    if info is None:
        return skipped(f"fixture unavailable: {fixture}")

    conn = _connect(dataset)
    try:
        scope = _scope(info)
        tenant = str(cfg.get("tenant_id", "default"))
        profile = str(cfg.get("profile", "usage"))
        versions = mining_version_ids(
            conn,
            scope["library"],
            scope["version"],
            scope["ecosystem"],
            tenant_id=tenant,
        )
        if len(versions) != 1:
            return skipped(f"version is not unique: {fixture}")
        version_id = versions[0]
        profiles = [
            int(row[0])
            for row in conn.execute(
                "SELECT id FROM mining_profiles WHERE version_id=? AND name=?"
                " ORDER BY id",
                (version_id, profile),
            )
        ]
        if not profiles:
            return skipped(f"profile unavailable: {fixture}/{profile}")

        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(call_instances)")
        }
        tenant_sql = ""
        entity_params: list[Any] = [version_id]
        if "tenant_id" in columns:
            tenant_sql = " AND ci.tenant_id = ?"
            entity_params.append(tenant)
        rows = conn.execute(
            "SELECT a.qualified_name, count(*) AS calls FROM call_instances ci"
            " JOIN api_entities a ON a.id=ci.callee_entity_id"
            f" WHERE ci.version_id=? AND ci.in_target_api=1{tenant_sql}"
            " GROUP BY a.id ORDER BY calls DESC, a.id LIMIT ?",
            [*entity_params, int(cfg["entity_limit"])],
        ).fetchall()
        entities = [str(row[0]) for row in rows]
        if not entities:
            return skipped(f"no hot entities for {fixture}")

        knobs = {
            "graph_max_depth": int(cfg.get("depth", 2)),
            "graph_query_budget": int(cfg.get("query_budget", 20000)),
            "graph_pattern_limit": int(cfg.get("pattern_limit", 200)),
            "graph_cache_max_edges": max_edges,
            "graph_cache_ttl_seconds": ttl,
        }
        sql_settings = _bench_settings(dataset, graph_cache_enabled=False, **knobs)
        cache_settings = _bench_settings(dataset, graph_cache_enabled=True, **knobs)
        ctx["settings"] = cache_settings
        sql_service = GraphQueryService(conn, sql_settings)
        cache_service = GraphQueryService(conn, cache_settings)

        queries: list[dict[str, Any]] = []
        for entity in entities:
            for direction in cfg["directions"]:
                queries.append(
                    {
                        "mode": "neighbors",
                        "entity": entity,
                        "direction": direction,
                        "depth": knobs["graph_max_depth"],
                        "library": scope["library"],
                        "version": scope["version"],
                        "ecosystem": scope["ecosystem"],
                        "tenant_id": tenant,
                        "profile": profile,
                    }
                )

        def run_sql() -> list[dict[str, Any]]:
            return [sql_service.query(**query) for query in queries]

        def run_cache() -> list[dict[str, Any]]:
            return [cache_service.query(**query) for query in queries]

        def run_cold() -> list[dict[str, Any]]:
            clear_graph_adjacency_caches()
            return run_cache()

        def traced(fn: Any) -> list[str]:
            captured: list[str] = []
            conn.set_trace_callback(captured.append)
            try:
                fn()
            finally:
                conn.set_trace_callback(None)
            return captured

        clear_graph_adjacency_caches()
        expected = run_sql()
        clear_graph_adjacency_caches()
        actual = run_cache()
        failures: list[str] = []
        if expected != actual:
            failures.append("SQL and cache adjacency results differ")

        graph_tables = (
            "api_entities",
            "call_instances",
            "review_status",
            "seq_patterns",
            "doc_api_links",
            "api_clusters",
            "feature_nodes",
            "import_edges",
            "documents",
            "doc_link_edges",
            "doc_pattern_coverage",
            "doc_consistency",
        )

        def adjacency_reads(statements: list[str]) -> int:
            return sum(
                1
                for statement in statements
                if any(table in statement.lower() for table in graph_tables)
            )

        sql_trace = traced(run_sql)
        clear_graph_adjacency_caches()
        scans_before = cache.stats["table_scans"]
        build_trace = traced(run_cache)
        scans_after = cache.stats["table_scans"]
        warm_trace = traced(run_cache)
        counters: dict[str, Any] = {
            "queries_per_batch": len(queries),
            "entities": len(entities),
            "sql_adjacency_reads": adjacency_reads(sql_trace),
            "cache_build_adjacency_reads": adjacency_reads(build_trace),
            "cache_build_table_scans": scans_after - scans_before,
            "cache_warm_adjacency_reads": adjacency_reads(warm_trace),
        }

        clear_graph_adjacency_caches()
        sql_measured = measure(
            run_sql,
            warmup=int(cfg["warmup"]),
            repeats=int(cfg["repeats"]),
        )
        clear_graph_adjacency_caches()
        cold_measured = measure(
            run_cold,
            warmup=0,
            repeats=int(cfg["repeats"]),
        )
        clear_graph_adjacency_caches()
        before = cache.stats
        warm_measured = measure(
            run_cache,
            warmup=int(cfg["warmup"]),
            repeats=int(cfg["repeats"]),
        )
        after = cache.stats
        sql_median = sql_measured["measured"]["stats"]["median_ms"]
        cold_median = cold_measured["measured"]["stats"]["median_ms"]
        warm_median = warm_measured["measured"]["stats"]["median_ms"]
        speedup = round(sql_median / warm_median, 3) if warm_median else 0.0
        delta = sql_median - warm_median
        amortized = round(cold_median / delta, 2) if delta > 0 else None
        warm_item = _result(
            "service.graph_adjacency",
            "cache_warm",
            warm_measured["measured"],
            counters={
                **counters,
                "sql_median_ms": sql_median,
                "cache_cold_median_ms": cold_median,
                "warm_speedup": speedup,
                "amortization_batches": amortized,
                "builds_during_warm_run": after["builds"] - before["builds"],
                "hits_during_warm_run": after["hits"] - before["hits"],
            },
            failures=failures,
            notes=[
                f"warm speedup {speedup}x vs SQL",
                f"cold build amortizes after {amortized} batches",
            ],
        )
        return [
            _result(
                "service.graph_adjacency",
                "sql",
                sql_measured["measured"],
                counters=counters,
                failures=failures,
            ),
            _result(
                "service.graph_adjacency",
                "cache_cold",
                cold_measured["measured"],
                counters=counters,
            ),
            warm_item,
        ]
    finally:
        conn.close()


CASES: dict[str, Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]] = {
    "micro.prefixspan": case_prefixspan,
    "micro.chunking": case_chunking,
    "micro.ast": case_ast,
    "micro.html_clean": case_html_clean,
    "pipeline.index": case_pipeline_index,
    "store.fts_scope": case_fts_scope,
    "store.mining_scan": case_mining_scan,
    "service.retrieval": case_service_retrieval,
    "service.graph_adjacency": case_graph_adjacency,
    "exit.web": case_exit_web,
}


def compare(
    cases: list[dict[str, Any]], baseline: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Attach same-machine relative verdicts to already measured cases."""
    if baseline is None:
        for case in cases:
            case["verdict"] = "no_baseline"
        return cases
    if baseline.get("environment") != environment():
        for case in cases:
            case["verdict"] = "incomparable"
        return cases
    old = {
        f"{item['case_id']}/{item['variant']}": item
        for item in baseline.get("cases", [])
    }
    for case in cases:
        key = f"{case['case_id']}/{case['variant']}"
        previous = old.get(key)
        if previous is None or not previous.get("stats", {}).get("median_ms"):
            case["verdict"] = "no_baseline"
            continue
        median = case.get("stats", {}).get("median_ms", 0.0)
        p95 = case.get("stats", {}).get("p95_ms", 0.0)
        if not median or not previous["stats"].get("median_ms"):
            case["verdict"] = "incomparable"
            continue
        band = max(2 * float(previous["stats"].get("cv", 0.0)), 0.10)
        p95_band = max(2 * float(previous["stats"].get("cv", 0.0)), 0.20)
        median_ratio = median / float(previous["stats"]["median_ms"])
        p95_ratio = p95 / float(previous["stats"].get("p95_ms") or p95 or 1.0)
        case["baseline_median_ms"] = previous["stats"]["median_ms"]
        case["median_ratio"] = round(median_ratio, 4)
        case["p95_ratio"] = round(p95_ratio, 4)
        if median_ratio > 1 + band or p95_ratio > 1 + p95_band:
            case["verdict"] = "needs_review"
        elif median_ratio < 1 - band:
            case["verdict"] = "improved"
        else:
            case["verdict"] = "tie"
    return cases


def write_report(report: dict[str, Any]) -> Path:
    """Write one structured report plus a readable summary from the same data."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = OUTPUTS / stamp
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Performance benchmark",
        "",
        f"- Gear: `{report['gear']}`",
        f"- Commit: `{report['environment']['commit']}`",
        f"- Python: {report['environment']['python']}",
        f"- CPU: {report['environment']['cpu']}",
        "",
        "| Case | Variant | Median (ms) | p95 (ms) | CV | Verdict |",
        "|---|---|---:|---:|---:|---|",
    ]
    for case in report["cases"]:
        item = case.get("stats") or {}
        lines.append(
            f"| {case['case_id']} | {case['variant']} | "
            f"{item.get('median_ms', 0):.4f} | {item.get('p95_ms', 0):.4f} | "
            f"{item.get('cv', 0):.4f} | {case.get('verdict', 'observed')} |"
        )
    (target / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUTPUTS / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target
