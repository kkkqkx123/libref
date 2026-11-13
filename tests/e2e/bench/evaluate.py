"""Evaluate retrieval over the real-project benchmark dataset.

Runs the production retrieval paths against the dataset built by `build.py`,
compares method families and parameter choices, probes the open design
questions (mining side-table cost, score scale, query shaping, material
budget units, confidence policy) and checks the versioned gates.

Usage:
    uv run python -m tests.e2e.bench.evaluate [--out DIR] [--gate-only] [--real]

Without `--real` the vector and rerank endpoints are deterministic stand-ins, so
the run is offline and repeatable. With `--real` the production transports call
the models configured in the repository environment file, which puts the fused
rows under their own quality floors (`real_floors` in `gates.json`).

Reports land under `outputs/` (not versioned); gates live in `gates.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
DATA = HERE / "data"
OUTPUT = HERE / "outputs" / "retrieval-bench"
JUDGMENTS = HERE / "judgments.json"
GATES = HERE / "gates.json"

CODE_URL_BASE = "repo://pypi/httpx/head"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run the retrieval benchmark")
    parser.add_argument("--out", default=str(OUTPUT))
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument(
        "--gate-only",
        action="store_true",
        help="check gates without writing a report",
    )
    parser.add_argument(
        "--no-probes",
        action="store_true",
        help="skip the design probes and only run the method matrix",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="use the real embedding and rerank models from the environment file",
    )
    return parser.parse_args()


def _doc_url(url: str) -> str:
    """Documentation judgments anchor on the crawled absolute URL."""
    return url


def _code_url(rel: str) -> str:
    return f"{CODE_URL_BASE}/{rel}"


def _load(judgments: Path = JUDGMENTS) -> dict:
    return json.loads(judgments.read_text(encoding="utf-8"))


def _gates(path: Path = GATES) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _store(conn, embedder=None, vectors=None):
    """Resolve the encoder and its store; sweeps and probes share one prepared store."""
    from tests.e2e.bench.fakes import BagOfTermsEmbedder, SqliteVecStore

    if embedder is None:
        embedder = BagOfTermsEmbedder()
    if vectors is None:
        vectors = SqliteVecStore(conn, embedder)
    return embedder, vectors


def _services(conn, settings, *, embedder, vectors, rerank=None):
    """Build the three method families over the same corpus."""
    from libref.services.docs.search import SearchService
    from tests.e2e.bench.fakes import lexical_rerank

    return {
        "precise": SearchService(conn, settings),
        "fused": SearchService(conn, settings, vec_store=vectors, embed=embedder),
        "fused_rerank": SearchService(
            conn,
            settings,
            vec_store=vectors,
            embed=embedder,
            rerank=rerank if rerank is not None else lexical_rerank(),
        ),
    }


def _version_id(conn, scope: dict[str, Any]) -> int:
    """Resolve the corpus version id from library, version and ecosystem."""
    row = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id "
        "WHERE l.name=? AND l.ecosystem=? AND v.version=?",
        (scope["library"], scope["ecosystem"], scope["version"]),
    ).fetchone()
    if row is None:
        raise SystemExit(f"corpus version not indexed: {scope}")
    return int(row[0])


def _duplicate_count(results: list[dict[str, Any]]) -> int:
    """Repeated material inside one result set: the same chunk, or one page's excerpt twice."""
    seen: set[tuple[str, str]] = set()
    duplicates = 0
    for item in results:
        identity = str(item.get("chunk_id") or "")
        key = (
            str(item.get("url") or ""),
            identity or str(item.get("snippet") or ""),
        )
        if key in seen:
            duplicates += 1
        seen.add(key)
    return duplicates


async def _evaluate_corpus(
    conn,
    settings,
    judgments: list[dict[str, Any]],
    url_of,
    scope: dict[str, Any],
    limit: int,
    *,
    embedder=None,
    vectors=None,
    rerank=None,
    methods: tuple[str, ...] = ("precise", "fused", "fused_rerank"),
) -> dict[str, Any]:
    """Run the selected methods over one corpus and collect per-query rank rows."""
    before = time.monotonic()
    embedder, vectors = _store(conn, embedder, vectors)
    await vectors.prepare()
    all_services = _services(
        conn, settings, embedder=embedder, vectors=vectors, rerank=rerank
    )
    services = {name: all_services[name] for name in methods}
    out: dict[str, Any] = {"build_ms": int((time.monotonic() - before) * 1000)}
    for method, service in services.items():
        preset = "precise" if method == "precise" else "balanced"
        rows: dict[str, list[dict[str, Any]]] = {}
        for judgment in judgments:
            try:
                results, _status = await service.search_with_mode(
                    judgment["query"],
                    preset,
                    **scope,
                    limit=limit,
                )
            except ValueError as exc:
                raise SystemExit(f"query '{judgment['id']}' rejected: {exc}") from exc
            rank = 0
            heading = judgment.get("heading")
            wanted = {url_of(target) for target in judgment.get("targets", [])}
            if wanted:
                for index, item in enumerate(results):
                    if str(item.get("url") or "") not in wanted:
                        continue
                    if heading and heading not in str(item.get("path") or ""):
                        continue
                    rank = index + 1
                    break
            rows.setdefault(str(judgment["type"]), []).append(
                {
                    "id": judgment["id"],
                    "rank": rank,
                    "count": len(results),
                    "duplicates": _duplicate_count(results),
                    "scores": [float(item.get("score") or 0.0) for item in results],
                }
            )
        flat = [row for group in rows.values() for row in group]
        out[method] = {"rows": flat, "by_type": rows}
    return out


def _aggregate(evaluation: dict[str, Any], k: int) -> dict[str, Any]:
    from tests.e2e.bench.metrics import summarize

    summary: dict[str, Any] = {}
    for method, payload in evaluation.items():
        if not isinstance(payload, dict) or "rows" not in payload:
            continue
        answerable = [
            row
            for name, group in payload["by_type"].items()
            if name != "absent"
            for row in group
        ]
        summary[method] = {
            "all": summarize(answerable, k),
            "by_type": {
                name: summarize(group, k)
                for name, group in payload["by_type"].items()
                if name != "absent"
            },
            "absent_empty_rate": summarize(payload["by_type"].get("absent", []), k)[
                "empty_rate"
            ],
        }
    return summary


def _probe_chunk_identity(conn) -> dict[str, Any]:
    """Stored chunk identity collisions: distinct rows sharing one page and chunk id."""
    rows = conn.execute(
        "SELECT url, json_extract(metadata, '$.chunk_id') AS cid, count(*) AS n "
        "FROM documents GROUP BY url, cid HAVING n > 1 ORDER BY n DESC LIMIT 5"
    ).fetchall()
    total = conn.execute(
        "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM documents "
        "GROUP BY url, json_extract(metadata, '$.chunk_id') HAVING n > 1)"
    ).fetchone()[0]
    return {
        "collided_rows": int(total),
        "worst": [
            {"url": row[0], "chunk_id": row[1], "rows": int(row[2])} for row in rows
        ],
    }


def _probe_side_tables(
    conn,
    settings,
    judgments: list[dict[str, Any]],
    scope: dict[str, Any],
    *,
    embedder,
    vectors,
) -> dict[str, Any]:
    """Mining side-table load: stored rows and per-query rows assembled across limits."""
    from libref.store import retriever as retriever_mod

    version_id = _version_id(conn, scope)
    patterns = int(
        conn.execute(
            "SELECT count(*) FROM seq_patterns WHERE version_id=?", (version_id,)
        ).fetchone()[0]
    )
    clusters = int(
        conn.execute(
            "SELECT count(*) FROM api_clusters WHERE version_id=?", (version_id,)
        ).fetchone()[0]
    )
    consulted = {"patterns": 0, "clusters": 0}
    original_patterns = retriever_mod._pattern_supports
    original_clusters = retriever_mod._cluster_sets

    def counting_patterns(conn, vids, wanted=None):
        out = original_patterns(conn, vids, wanted)
        consulted["patterns"] = sum(len(items) for items in out.values())
        return out

    def counting_clusters(conn, vids):
        out = original_clusters(conn, vids)
        consulted["clusters"] = sum(len(items) for items in out.values())
        return out

    retriever_mod._pattern_supports = counting_patterns
    retriever_mod._cluster_sets = counting_clusters
    per_limit: dict[str, Any] = {}
    services = _services(conn, settings, embedder=embedder, vectors=vectors)
    try:
        for limit in (5, 20):
            pattern_rows: list[int] = []
            cluster_rows: list[int] = []
            samples: list[float] = []
            for judgment in judgments:
                if judgment.get("type") == "absent":
                    continue
                consulted["patterns"] = 0
                consulted["clusters"] = 0
                started = time.monotonic()
                asyncio.run(
                    services["fused"].search_with_mode(
                        judgment["query"], "balanced", **scope, limit=limit
                    )
                )
                samples.append(time.monotonic() - started)
                pattern_rows.append(consulted["patterns"])
                cluster_rows.append(consulted["clusters"])
            per_limit[f"limit_{limit}"] = {
                "median_ms": round(statistics.median(samples) * 1000, 2),
                "median_patterns": statistics.median(pattern_rows),
                "median_clusters": statistics.median(cluster_rows),
            }
    finally:
        retriever_mod._pattern_supports = original_patterns
        retriever_mod._cluster_sets = original_clusters
    return {
        "seq_patterns": patterns,
        "api_cluster_rows": clusters,
        "per_limit": per_limit,
    }


def _probe_score_scale(evaluation: dict[str, Any]) -> dict[str, Any]:
    from tests.e2e.bench.metrics import distribution

    out: dict[str, Any] = {}
    for method, payload in evaluation.items():
        if not isinstance(payload, dict) or "rows" not in payload:
            continue
        scores = [score for row in payload["rows"] for score in row["scores"]]
        out[method] = distribution(scores)
    return out


def _probe_query_shaping(
    conn, settings, judgments: list[dict[str, Any]], scope: dict[str, Any], limit: int
) -> dict[str, Any]:
    """Compare the current OR bag against an all-tokens-required variant."""
    from libref.store.retriever import fts_search
    from libref.text.query import safe_fts_query

    def _weights() -> tuple[float, float, float, float]:
        return (
            float(settings.fts_w_content),
            float(settings.fts_w_title),
            float(settings.fts_w_url),
            float(settings.fts_w_path),
        )

    def _and_matches(query: str) -> list[str]:
        shaped = safe_fts_query(query)
        if not shaped:
            return []
        and_query = shaped.replace(" OR ", " AND ")
        weights = _weights()
        rows = conn.execute(
            "SELECT documents_fts.rowid FROM documents_fts"
            f" WHERE documents_fts MATCH ? AND rowid IN"
            f" (SELECT id FROM documents WHERE version_id=?)"
            f" ORDER BY bm25(documents_fts, {weights[0]}, {weights[1]},"
            f" {weights[2]}, {weights[3]}) LIMIT ?",
            [and_query, _version_id(conn, scope), limit],
        ).fetchall()
        urls = []
        for (doc_id,) in rows:
            row = conn.execute(
                "SELECT url FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            if row is not None:
                urls.append(str(row[0]))
        return urls

    variants = {"or_tokens": [], "and_tokens": []}
    for judgment in judgments:
        wanted_set = {_doc_url(target) for target in judgment.get("targets", [])}
        if not wanted_set:
            continue
        heading = judgment.get("heading")
        pairs = fts_search(
            conn,
            judgment["query"],
            **scope,
            weights=_weights(),
            budget=limit,
        )
        or_urls = []
        for doc_id, _score in pairs:
            row = conn.execute(
                "SELECT url, metadata FROM documents WHERE id=?", (doc_id,)
            ).fetchone()
            if row is not None:
                or_urls.append((str(row[0]), str(row[1] or "")))
        rank = 0
        for index, (url, meta) in enumerate(or_urls):
            if url in wanted_set and (not heading or heading in meta):
                rank = index + 1
                break
        variants["or_tokens"].append({"rank": rank})
        rank = 0
        for index, url in enumerate(_and_matches(judgment["query"])):
            if url in wanted_set:
                rank = index + 1
                break
        variants["and_tokens"].append({"rank": rank})
    return variants


def _probe_material_units(
    conn, settings, judgments: list[dict[str, Any]], scope: dict[str, Any]
) -> dict[str, Any]:
    """Material budget units: character cap versus the token convention."""
    from libref.chunking.budget import estimate_tokens
    from libref.services.docs.answer_materials import apply_total_budget
    from libref.services.docs.search import SearchService
    from libref.store.document_store import DocumentStore

    service = SearchService(conn, settings)
    store = DocumentStore(conn)
    rows: list[dict[str, float]] = []
    for judgment in judgments:
        if judgment.get("type") == "absent":
            continue
        results, _status = asyncio.run(
            service.search_with_mode(judgment["query"], "precise", **scope, limit=5)
        )
        if not results:
            continue
        found = store.get_documents([int(r["id"]) for r in results])
        bodies = {int(r["id"]): str(r.get("content") or "") for r in found}
        kept, budgeted, omitted = apply_total_budget(
            results,
            bodies,
            int(settings.material_limit),
            int(settings.answer_total_budget),
        )
        chars = sum(len(body) for body in budgeted.values())
        tokens = sum(estimate_tokens(body) for body in budgeted.values())
        rows.append(
            {
                "kept": float(len(kept)),
                "omitted": float(omitted),
                "chars": float(chars),
                "tokens": float(tokens),
                "tokens_per_char": tokens / chars if chars else 0.0,
            }
        )
    if not rows:
        return {"queries": 0}
    return {
        "queries": len(rows),
        "material_limit": int(settings.material_limit),
        "answer_total_budget": int(settings.answer_total_budget),
        "median_chars": statistics.median(r["chars"] for r in rows),
        "median_tokens": statistics.median(r["tokens"] for r in rows),
        "max_tokens": max(r["tokens"] for r in rows),
        "median_tokens_per_char": statistics.median(r["tokens_per_char"] for r in rows),
        "median_omitted": statistics.median(r["omitted"] for r in rows),
    }


def _probe_confidence(
    conn, settings, judgments: list[dict[str, Any]], scope: dict[str, Any]
) -> dict[str, Any]:
    """Confidence policy: replay three answer shapes against the current materials.

    The three cases are policy fixtures, not recorded model output: cite the
    right material, cite the wrong material, cite nothing. A policy that only
    looks at "any citation" cannot separate the first two.
    """
    from libref.services.docs.answer_materials import filter_citations_by_answer
    from libref.services.docs.search import SearchService

    service = SearchService(conn, settings)
    cases: list[dict[str, Any]] = []
    for judgment in judgments:
        wanted_set = {_doc_url(target) for target in judgment.get("targets", [])}
        if judgment.get("type") == "absent" or not wanted_set:
            continue
        results, _status = asyncio.run(
            service.search_with_mode(judgment["query"], "precise", **scope, limit=5)
        )
        if len(results) < 2:
            continue
        right = next(
            (
                index + 1
                for index, item in enumerate(results)
                if str(item.get("url") or "") in wanted_set
            ),
            0,
        )
        if not right:
            continue
        wrong = 1 if right != 1 else 2
        for label, answer in (
            ("grounded", f"结论 [{right}]"),
            ("wrong_citation", f"结论 [{wrong}]"),
            ("uncited", "结论"),
        ):
            _kept, cited = filter_citations_by_answer(results, answer)
            cases.append(
                {
                    "id": judgment["id"],
                    "label": label,
                    "policy": "high" if cited else "low",
                }
            )
    total = len(cases)
    false_high = sum(
        1 for case in cases if case["label"] != "grounded" and case["policy"] == "high"
    )
    return {
        "cases": total,
        "false_high": false_high,
        "false_high_rate": false_high / total if total else 0.0,
        "high_on_grounded": all(
            case["policy"] == "high" for case in cases if case["label"] == "grounded"
        ),
    }


def _probe_fusion_rank_loss(
    conn,
    settings,
    judgments: list[dict[str, Any]],
    scope: dict[str, Any],
    limit: int,
    *,
    embedder,
    vectors,
) -> dict[str, Any]:
    """Does fusion keep the full-text top pick? Counts page-level survival by query type."""
    services = _services(conn, settings, embedder=embedder, vectors=vectors)
    rows: list[dict[str, Any]] = []
    for judgment in judgments:
        if judgment.get("type") == "absent":
            continue
        precise, _ = asyncio.run(
            services["precise"].search_with_mode(
                judgment["query"], "precise", **scope, limit=limit
            )
        )
        fused, _ = asyncio.run(
            services["fused"].search_with_mode(
                judgment["query"], "balanced", **scope, limit=limit
            )
        )
        if not precise:
            continue
        top_url = str(precise[0].get("url") or "")
        kept = any(str(item.get("url") or "") == top_url for item in fused)
        rows.append({"id": judgment["id"], "type": judgment["type"], "kept": kept})
    total = len(rows)
    lost = [row["id"] for row in rows if not row["kept"]]
    by_type: dict[str, list[int]] = {}
    for row in rows:
        bucket = by_type.setdefault(str(row["type"]), [0, 0])
        bucket[0] += 1
        bucket[1] += 0 if row["kept"] else 1
    return {
        "queries": total,
        "top1_lost": len(lost),
        "lost_ids": lost,
        "by_type": {
            name: {"queries": counts[0], "lost": counts[1]}
            for name, counts in by_type.items()
        },
    }


def _check_gates(
    summary: dict[str, Any], gates: dict[str, Any], *, real: bool
) -> list[str]:
    """Gate the full-text path always, and the model paths only under real models.

    The full-text path is production-representative in both modes. Fused rows
    only get absolute floors under `--real`: with the lexical stand-ins their
    quality reflects the stand-in, not the retrieval design.
    """
    failures: list[str] = []
    max_duplicate = float(gates["invariants"]["max_duplicate_rate"])
    for corpus, methods in summary.items():
        if not isinstance(methods, dict):
            continue
        precise = methods.get("precise", {}).get("all", {})
        floors = dict(gates["precise_floors"].get(corpus, {}))
        if real:
            for method, metrics in gates.get("real_floors", {}).get(corpus, {}).items():
                actual = methods.get(method, {}).get("all", {})
                for metric, floor in metrics.items():
                    if actual.get(metric, 0.0) < float(floor):
                        failures.append(
                            f"{corpus}/{method}: {metric}={actual.get(metric, 0.0):.3f}"
                            f" below floor {float(floor):.3f}"
                        )
        for metric, floor in floors.items():
            if precise.get(metric, 0.0) < float(floor):
                failures.append(
                    f"{corpus}/precise: {metric}={precise.get(metric, 0.0):.3f}"
                    f" below floor {float(floor):.3f}"
                )
        for method, payload in methods.items():
            duplicates = payload.get("all", {}).get("duplicate_rate", 0.0)
            if duplicates > max_duplicate:
                failures.append(
                    f"{corpus}/{method}: duplicate_rate={duplicates:.3f}"
                    f" exceeds {max_duplicate:.3f}"
                )
    return failures


def _report(
    manifest: dict[str, Any],
    summary: dict[str, Any],
    probes: dict[str, Any],
    failures: list[str],
    out_dir: Path,
    k: int,
    models: str | None = None,
) -> Path:
    lines: list[str] = ["# 检索问答基准报告（真实项目夹具）", ""]
    if models:
        lines.append(f"- {models}")
    lines.append(f"语料根：`{manifest.get('root')}`")
    for name, info in manifest.get("fixtures", {}).items():
        lines.append(
            f"- {name}: library={info.get('library')} version={info.get('version')} "
            + ", ".join(
                f"{key}={value}"
                for key, value in info.items()
                if key
                not in {
                    "kind",
                    "library",
                    "version",
                    "library_id",
                    "version_id",
                    "subdir",
                }
            )
        )
    lines += ["", f"## 方法矩阵（Recall/MRR/nDCG @{k}）", ""]
    for corpus, methods in summary.items():
        if not isinstance(methods, dict):
            continue
        lines += [f"### {corpus}", ""]
        lines.append("| 方法 | recall@k | MRR@k | nDCG@k | 空结果率 | 重复率 |")
        lines.append("|---|---|---|---|---|---|")
        for method, payload in methods.items():
            all_metrics = payload.get("all", {})
            lines.append(
                f"| {method} | {all_metrics.get('recall_at_k', 0):.3f} "
                f"| {all_metrics.get('mrr_at_k', 0):.3f} "
                f"| {all_metrics.get('ndcg_at_k', 0):.3f} "
                f"| {all_metrics.get('empty_rate', 0):.3f} "
                f"| {all_metrics.get('duplicate_rate', 0):.3f} |"
            )
        lines.append("")
        lines.append("| 方法 | 类型 | recall@k | MRR@k |")
        lines.append("|---|---|---|---|")
        for method, payload in methods.items():
            for query_type, metrics in payload.get("by_type", {}).items():
                lines.append(
                    f"| {method} | {query_type} "
                    f"| {metrics.get('recall_at_k', 0):.3f} "
                    f"| {metrics.get('mrr_at_k', 0):.3f} |"
                )
        lines.append("")
    lines += ["## 专项探针", ""]
    identity = probes.get("chunk_identity", {})
    lines += [
        "### 存储块标识",
        "",
        f"- 同页同 chunk_id 的多行合计 {identity.get('collided_rows')} 行",
    ]
    for item in identity.get("worst", []):
        lines.append(f"  - {item.get('rows')} 行：`{item.get('chunk_id')}`")
    side = probes.get("side_tables", {})
    lines += [
        "### 挖掘副表加载成本",
        "",
        (
            f"- 存储规模：seq_patterns={side.get('seq_patterns')} 行，"
            f"api_cluster={side.get('api_cluster_rows')} 行"
        ),
        "- 每次查询装配（中位）与耗时：",
        "",
        "| 条数上限 | pattern 条数 | cluster 条数 | 中位耗时 |",
        "|---|---|---|---|",
    ]
    for name, stats in side.get("per_limit", {}).items():
        lines.append(
            f"| {name} | {stats.get('median_patterns')} | "
            f"{stats.get('median_clusters')} | {stats.get('median_ms')}ms |"
        )
    lines += [
        "",
        "### 打分尺度",
        "",
        "| 语料 | 方法 | min | median | max |",
        "|---|---|---|---|---|",
    ]
    for corpus, methods in probes.get("score_scale", {}).items():
        for method, stats in methods.items():
            lines.append(
                f"| {corpus} | {method} | {stats.get('min', 0):.4f} "
                f"| {stats.get('median', 0):.4f} | {stats.get('max', 0):.4f} |"
            )
    loss = probes.get("fusion_rank_loss", {})
    lines += [
        "",
        "### 融合对全文首位结果的保留",
        "",
        (
            f"- 全文首位页面在融合前 {k} 条内保留 "
            f"{loss.get('queries', 0) - loss.get('top1_lost', 0)}"
            f"/{loss.get('queries', 0)}，"
            f"丢失 {loss.get('top1_lost', 0)} 条："
            f"{', '.join(loss.get('lost_ids', [])) or '无'}"
        ),
    ]
    for name, counts in loss.get("by_type", {}).items():
        lines.append(f"- {name}: 丢失 {counts.get('lost')}/{counts.get('queries')}")
    shaping = probes.get("query_shaping", {})
    lines += [
        "",
        "### 查询整形变体",
        "",
        "| 变体 | recall@k | MRR@k |",
        "|---|---|---|",
    ]
    for name, rows in shaping.items():
        metrics = _shape_metrics(rows, k)
        lines.append(
            f"| {name} | {metrics['recall_at_k']:.3f} | {metrics['mrr_at_k']:.3f} |"
        )
    units = probes.get("material_units", {})
    lines += [
        "",
        "### 材料预算单位",
        "",
        (
            f"- 配置：单材料 {units.get('material_limit')} 字符，总预算 "
            f"{units.get('answer_total_budget')} 字符"
        ),
        (
            f"- 实测：中位 {units.get('median_chars')} 字符 / "
            f"{units.get('median_tokens')} 估计令牌，最大 "
            f"{units.get('max_tokens')} 估计令牌"
        ),
        (
            f"- 令牌字符比中位 {units.get('median_tokens_per_char', 0):.3f}；"
            f"中位被丢弃材料 {units.get('median_omitted')} 条"
        ),
    ]
    confidence = probes.get("confidence", {})
    lines += [
        "",
        "### 置信度策略",
        "",
        (
            f"- 回放用例 {confidence.get('cases')} 个；"
            f"误报高置信 {confidence.get('false_high')} 个"
            f"（{confidence.get('false_high_rate', 0):.3f}）"
        ),
        f"- 正确引用材料时是否全为高置信：{confidence.get('high_on_grounded')}",
    ]
    lines += ["", "## 门禁结论", ""]
    if failures:
        lines.append("FAIL")
        for failure in failures:
            lines.append(f"- {failure}")
    else:
        lines.append("PASS：全部下限与差分约束满足。")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "manifest": manifest,
        "summary": summary,
        "probes": probes,
        "failures": failures,
    }
    (out_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def _shape_metrics(rows: list[dict[str, Any]], k: int) -> dict[str, float]:
    from tests.e2e.bench.metrics import summarize

    return summarize(rows, k)


def run(
    data_dir: Path = DATA,
    out_dir: Path = OUTPUT,
    probes_enabled: bool = True,
    real: bool = False,
) -> dict[str, Any]:
    """Evaluate the dataset and write the report and results payload."""
    from libref.db import open_db
    from tests.e2e.bench.fakes import (
        SqliteVecStore,
        bench_settings,
        real_embedder,
        real_rerank,
        real_settings,
    )

    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("skipped"):
        raise SystemExit("fixtures unavailable: " + "; ".join(manifest["skipped"]))
    judgments = _load()
    gates = _gates()
    limit = int(gates["top_k"])
    conn = open_db(data_dir / "bench.db")
    if real:
        settings = real_settings(data_dir / "bench.db")
        embedder = real_embedder(settings)
        rerank = real_rerank(settings)
        vectors = SqliteVecStore(conn, embedder)
    else:
        settings = bench_settings(data_dir / "bench.db")
        rerank = None
        embedder, vectors = _store(conn, None, None)
    asyncio.run(vectors.prepare())
    scopes = {
        name: {
            "library": info["library"],
            "version": info["version"],
            "ecosystem": info.get("ecosystem", "custom"),
        }
        for name, info in manifest["fixtures"].items()
    }
    try:
        docs_eval = asyncio.run(
            _evaluate_corpus(
                conn,
                settings,
                judgments["docs"],
                _doc_url,
                scopes["docs"],
                limit,
                embedder=embedder,
                vectors=vectors,
                rerank=rerank,
            )
        )
        code_eval = asyncio.run(
            _evaluate_corpus(
                conn,
                settings,
                judgments["code"],
                _code_url,
                scopes["code"],
                limit,
                embedder=embedder,
                vectors=vectors,
                rerank=rerank,
            )
        )
        summary = {
            "docs": _aggregate(docs_eval, limit),
            "code": _aggregate(code_eval, limit),
        }
        probes: dict[str, Any] = {}
        if probes_enabled:
            probes = {
                "chunk_identity": _probe_chunk_identity(conn),
                "side_tables": _probe_side_tables(
                    conn,
                    settings,
                    judgments["code"],
                    scopes["code"],
                    embedder=embedder,
                    vectors=vectors,
                ),
                "score_scale": {
                    "docs": _probe_score_scale(docs_eval),
                    "code": _probe_score_scale(code_eval),
                },
                "fusion_rank_loss": _probe_fusion_rank_loss(
                    conn,
                    settings,
                    judgments["docs"],
                    scopes["docs"],
                    limit,
                    embedder=embedder,
                    vectors=vectors,
                ),
                "query_shaping": _probe_query_shaping(
                    conn, settings, judgments["docs"], scopes["docs"], limit
                ),
                "material_units": _probe_material_units(
                    conn, settings, judgments["docs"], scopes["docs"]
                ),
                "confidence": _probe_confidence(
                    conn, settings, judgments["docs"], scopes["docs"]
                ),
            }
        failures = _check_gates(summary, gates, real=real)
        _report(
            manifest,
            summary,
            probes,
            failures,
            out_dir,
            limit,
            models=_model_line(settings, real),
        )
    finally:
        conn.close()
    return {"summary": summary, "probes": probes, "failures": failures}


def _model_line(settings, real: bool) -> str:
    """One line stating which endpoints produced the numbers."""
    if not real:
        return "向量与重排：离线替身（词袋伪向量、词覆盖重排）"
    return (
        f"向量：{settings.embedding_model}（{settings.embedding_dim} 维）；"
        f"重排：{settings.rerank_model}，候选 {settings.rerank_candidate_limit} 条、"
        f"保留 {settings.rerank_top_n} 条、输入截断 {settings.rerank_truncate} 字符"
    )


def main() -> int:
    args = _parse_args()
    result = run(
        Path(args.data),
        Path(args.out),
        probes_enabled=not args.no_probes,
        real=args.real,
    )
    if not args.gate_only:
        print(f"report: {Path(args.out) / 'report.md'}")
    for failure in result["failures"]:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
