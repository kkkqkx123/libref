"""Parameter sweep over the real-project benchmark dataset.

Answers "how should the knobs be set" with measurements instead of intuition:
retrieval-time knobs are swept over the existing dataset, indexing-time knobs
(chunk budgets) rebuild their own dataset because chunking happens at build
time.

Usage:
    uv run python -m tests.e2e.bench.sweep [--out DIR] [--group NAME]... [--real]

Without `--real` the vector and rerank endpoints are the deterministic stand-ins;
with it they are the real models from the repository environment file, which is
what makes the fusion and rerank knobs calibrated rather than merely wired.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUTPUT = HERE / "outputs" / "retrieval-bench"

# name -> settings overrides; the first entry of each group is the shipped default.
RETRIEVAL_VARIANTS: list[dict[str, Any]] = [
    {
        "group": "fts_weights",
        "name": "title5_path3 (default)",
        "overrides": {},
        "kinds": ("docs",),
        "methods": ("precise",),
    },
    {
        "group": "fts_weights",
        "name": "title10_path3",
        "overrides": {"fts_w_title": 10.0},
        "kinds": ("docs",),
        "methods": ("precise",),
    },
    {
        "group": "fts_weights",
        "name": "title1_path3",
        "overrides": {"fts_w_title": 1.0},
        "kinds": ("docs",),
        "methods": ("precise",),
    },
    {
        "group": "fts_weights",
        "name": "title5_path1",
        "overrides": {"fts_w_path": 1.0},
        "kinds": ("docs",),
        "methods": ("precise",),
    },
    {
        "group": "fts_weights",
        "name": "title5_path8",
        "overrides": {"fts_w_path": 8.0},
        "kinds": ("docs",),
        "methods": ("precise",),
    },
    {
        "group": "rrf",
        "name": "k20_vec1.0 (default)",
        "overrides": {},
        "kinds": ("docs", "code"),
        "methods": ("fused", "fused_rerank"),
    },
    {
        "group": "rrf",
        "name": "k10_vec1.0",
        "overrides": {"rrf_k": 10},
        "kinds": ("docs", "code"),
        "methods": ("fused", "fused_rerank"),
    },
    {
        "group": "rrf",
        "name": "k60_vec1.0",
        "overrides": {"rrf_k": 60},
        "kinds": ("docs", "code"),
        "methods": ("fused", "fused_rerank"),
    },
    {
        "group": "rrf",
        "name": "k20_vec0.5",
        "overrides": {"rrf_w_vec": 0.5},
        "kinds": ("docs", "code"),
        "methods": ("fused", "fused_rerank"),
    },
    {
        "group": "rrf",
        "name": "k20_vec0.25",
        "overrides": {"rrf_w_vec": 0.25},
        "kinds": ("docs", "code"),
        "methods": ("fused", "fused_rerank"),
    },
    {
        "group": "rrf",
        "name": "k20_vec1.5",
        "overrides": {"rrf_w_vec": 1.5},
        "kinds": ("docs", "code"),
        "methods": ("fused", "fused_rerank"),
    },
    {
        "group": "recall_budget",
        "name": "mult3_floor30 (default)",
        "overrides": {},
        "kinds": ("docs", "code"),
        "methods": ("fused",),
    },
    {
        "group": "recall_budget",
        "name": "mult1_floor10",
        "overrides": {"candidate_multiplier": 1, "rerank_candidate_limit": 10},
        "kinds": ("docs", "code"),
        "methods": ("fused",),
    },
    {
        "group": "recall_budget",
        "name": "mult6_floor60",
        "overrides": {"candidate_multiplier": 6, "rerank_candidate_limit": 60},
        "kinds": ("docs", "code"),
        "methods": ("fused",),
    },
    {
        "group": "assemble_window",
        "name": "mult2_min10 (default)",
        "overrides": {},
        "kinds": ("docs", "code"),
        "methods": ("fused",),
    },
    {
        "group": "assemble_window",
        "name": "mult1_min5",
        "overrides": {"assemble_multiplier": 1, "assemble_min": 5},
        "kinds": ("docs", "code"),
        "methods": ("fused",),
    },
    {
        "group": "assemble_window",
        "name": "mult4_min20",
        "overrides": {"assemble_multiplier": 4, "assemble_min": 20},
        "kinds": ("docs", "code"),
        "methods": ("fused",),
    },
    {
        "group": "rerank_depth",
        "name": "top_n10 (default)",
        "overrides": {},
        "kinds": ("docs", "code"),
        "methods": ("fused_rerank",),
    },
    {
        "group": "rerank_depth",
        "name": "top_n5",
        "overrides": {"rerank_top_n": 5},
        "kinds": ("docs", "code"),
        "methods": ("fused_rerank",),
    },
    {
        "group": "rerank_depth",
        "name": "top_n30",
        "overrides": {"rerank_top_n": 30},
        "kinds": ("docs", "code"),
        "methods": ("fused_rerank",),
    },
    {
        "group": "mining_weight",
        "name": "alpha_0.15_0.05 (default)",
        "overrides": {},
        "kinds": ("code",),
        "methods": ("fused",),
    },
    {
        "group": "mining_weight",
        "name": "alpha_0_0",
        "overrides": {"freq_alpha": 0.0, "cluster_alpha": 0.0},
        "kinds": ("code",),
        "methods": ("fused",),
    },
    {
        "group": "mining_weight",
        "name": "alpha_0.3_0.2",
        "overrides": {"freq_alpha": 0.3, "cluster_alpha": 0.2},
        "kinds": ("code",),
        "methods": ("fused",),
    },
    {
        "group": "mining_weight",
        "name": "cluster_only_0.2",
        "overrides": {"freq_alpha": 0.0, "cluster_alpha": 0.2},
        "kinds": ("code",),
        "methods": ("fused",),
    },
]

# Indexing-time variants: each rebuilds its own data set, docs corpus only.
CHUNKING_VARIANTS: list[dict[str, Any]] = [
    {"group": "chunk_bm25", "name": "150_45 (default)", "overrides": {}},
    {
        "group": "chunk_bm25",
        "name": "80_30",
        "overrides": {"chunk_bm25_max_terms": 80, "chunk_bm25_min_terms": 30},
    },
    {
        "group": "chunk_bm25",
        "name": "300_90",
        "overrides": {"chunk_bm25_max_terms": 300, "chunk_bm25_min_terms": 90},
    },
    {
        "group": "chunk_embed",
        "name": "512_150 (default)",
        "overrides": {},
    },
    {
        "group": "chunk_embed",
        "name": "320_96",
        "overrides": {
            "chunk_embed_max_tokens": 320,
            "chunk_embed_min_tokens": 96,
            "chunk_embed_overlap_tokens": 32,
        },
    },
    {
        "group": "chunk_embed",
        "name": "256_80",
        "overrides": {
            "chunk_embed_max_tokens": 256,
            "chunk_embed_min_tokens": 80,
            "chunk_embed_overlap_tokens": 32,
        },
    },
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sweep retrieval parameters")
    parser.add_argument("--out", default=str(OUTPUT))
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument(
        "--group", action="append", default=None, help="restrict to one or more groups"
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="use the real embedding and rerank models from the environment file",
    )
    return parser.parse_args()


def _fixture_ids(fixtures: dict[str, Any], kinds: tuple[str, ...]) -> list[str]:
    """Fixture ids of the requested kinds, in manifest order."""
    return [name for name, info in fixtures.items() if info["kind"] in kinds]


def _run_retrieval_variants(
    data_dir: Path, groups: set[str] | None, real: bool
) -> list[dict[str, Any]]:
    from libref.db import open_db
    from tests.e2e.bench.evaluate import (
        _aggregate,
        _evaluate_corpus,
        _load,
        _url_for,
    )
    from tests.e2e.bench.fakes import (
        BagOfTermsEmbedder,
        SqliteVecStore,
        bench_settings,
        real_embedder,
        real_rerank,
        real_settings,
    )

    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    judgments = _dev_only(_load())
    fixtures = manifest["fixtures"]
    scopes = {
        name: {
            "library": info["library"],
            "version": info["version"],
            "ecosystem": info["ecosystem"],
        }
        for name, info in fixtures.items()
    }
    urls = {name: _url_for(info) for name, info in fixtures.items()}
    conn = open_db(data_dir / "bench.db")
    base_settings = (
        real_settings(data_dir / "bench.db")
        if real
        else bench_settings(data_dir / "bench.db")
    )
    if real:
        embedder = real_embedder(base_settings)
        rerank = real_rerank(base_settings)
    else:
        embedder = BagOfTermsEmbedder()
        rerank = None
    vectors = SqliteVecStore(conn, embedder)
    asyncio.run(vectors.prepare())
    rows: list[dict[str, Any]] = []
    series: dict[tuple[str, str, str, str], dict[str, list[float]]] = {}
    try:
        for variant in RETRIEVAL_VARIANTS:
            if groups and variant["group"] not in groups:
                continue
            model = real_settings if real else bench_settings
            settings = model(data_dir / "bench.db", **variant["overrides"])
            started = time.monotonic()
            for corpus in _fixture_ids(fixtures, variant["kinds"]):
                evaluation = asyncio.run(
                    _evaluate_corpus(
                        conn,
                        settings,
                        judgments[corpus],
                        urls[corpus],
                        scopes[corpus],
                        5,
                        embedder=embedder,
                        vectors=vectors,
                        rerank=rerank,
                        methods=variant["methods"],
                    )
                )
                summary = _aggregate(evaluation, 5)
                for method in variant["methods"]:
                    metrics = summary[method]["dev"]
                    series[(variant["group"], variant["name"], corpus, method)] = (
                        _series(evaluation[method], 5)
                    )
                    rows.append(
                        {
                            "group": variant["group"],
                            "name": variant["name"],
                            "corpus": corpus,
                            "method": method,
                            "recall_at_k": metrics["recall_at_k"],
                            "mrr_at_k": metrics["mrr_at_k"],
                            "ndcg_at_k": metrics["ndcg_at_k"],
                            "duplicate_rate": metrics["duplicate_rate"],
                        }
                    )
            elapsed = int((time.monotonic() - started) * 1000)
            for row in rows:
                if row["group"] == variant["group"] and row["name"] == variant["name"]:
                    row["elapsed_ms"] = elapsed
    finally:
        conn.close()
    _annotate(rows, series)
    return rows


def _run_chunking_variants(groups: set[str] | None, real: bool) -> list[dict[str, Any]]:
    from libref.db import open_db
    from tests.e2e.bench import archive as bench_archive
    from tests.e2e.bench.build import build
    from tests.e2e.bench.evaluate import _aggregate, _evaluate_corpus, _load, _url_for
    from tests.e2e.bench.fakes import (
        BagOfTermsEmbedder,
        bench_settings,
        real_embedder,
        real_rerank,
        real_settings,
    )

    judgments = _dev_only(_load())
    docs_ids = [
        bench_archive.fixture_id(entry)
        for entry in bench_archive.select()
        if entry["kind"] == "docs"
    ]
    if not docs_ids:
        raise SystemExit("spec holds no documents fixture to sweep chunk budgets on")
    # Indexing knobs rebuild the dataset, so they are swept on the first
    # documents fixture in spec order rather than on every project.
    docs_id = docs_ids[0]
    rows: list[dict[str, Any]] = []
    series: dict[tuple[str, str, str, str], dict[str, list[float]]] = {}
    for variant in CHUNKING_VARIANTS:
        if groups and variant["group"] not in groups:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "data"
            started = time.monotonic()
            manifest = build(
                out_dir,
                names=[docs_id],
                settings_overrides=variant["overrides"],
            )
            corpus = manifest["fixtures"][docs_id]
            scope = {
                "library": corpus["library"],
                "version": corpus["version"],
                "ecosystem": corpus["ecosystem"],
            }
            model = real_settings if real else bench_settings
            settings = model(out_dir / "bench.db", **variant["overrides"])
            if real:
                embedder = real_embedder(settings)
                rerank = real_rerank(settings)
            else:
                embedder = BagOfTermsEmbedder()
                rerank = None
            conn = open_db(out_dir / "bench.db")
            try:
                evaluation = asyncio.run(
                    _evaluate_corpus(
                        conn,
                        settings,
                        judgments[docs_id],
                        _url_for(corpus),
                        scope,
                        5,
                        embedder=embedder,
                        rerank=rerank,
                        methods=("precise", "fused"),
                    )
                )
                summary = _aggregate(evaluation, 5)
                for method in ("precise", "fused"):
                    metrics = summary[method]["dev"]
                    series[(variant["group"], variant["name"], docs_id, method)] = (
                        _series(evaluation[method], 5)
                    )
                    rows.append(
                        {
                            "group": variant["group"],
                            "name": variant["name"],
                            "corpus": docs_id,
                            "method": method,
                            "recall_at_k": metrics["recall_at_k"],
                            "mrr_at_k": metrics["mrr_at_k"],
                            "ndcg_at_k": metrics["ndcg_at_k"],
                            "duplicate_rate": metrics["duplicate_rate"],
                            "chunks": corpus["chunks"],
                            "pages": corpus["pages"],
                            "elapsed_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
            finally:
                conn.close()
    _annotate(rows, series)
    return rows


def _dev_only(judgments: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Tuning reads the dev half only; the held-out half belongs to the gates."""
    return {
        corpus: [entry for entry in entries if entry["split"] == "dev"]
        for corpus, entries in judgments.items()
    }


def _series(evaluation: dict[str, Any], k: int) -> dict[str, list[float]]:
    from tests.e2e.bench.metrics import metric_series

    rows = [
        row
        for name, group in evaluation["by_type"].items()
        if name != "absent"
        for row in group
    ]
    return metric_series(rows, k)


def _annotate(
    rows: list[dict[str, Any]],
    series: dict[tuple[str, str, str, str], dict[str, list[float]]],
) -> None:
    """Attach each variant's difference against its group's shipped default.

    Recall and MRR are both carried: a weight change usually moves ranks inside
    the top k, which recall cannot see, while an indexing change moves recall.
    """
    from tests.e2e.bench.metrics import paired_delta

    baseline: dict[str, str] = {}
    for row in rows:
        baseline.setdefault(row["group"], row["name"])
    for row in rows:
        base_name = baseline[row["group"]]
        key = (row["group"], row["name"], row["corpus"], row["method"])
        base_key = (row["group"], base_name, row["corpus"], row["method"])
        if key not in series or base_key not in series:
            continue
        for metric in ("recall", "mrr"):
            delta = paired_delta(series[key][metric], series[base_key][metric])
            row[f"delta_{metric}"] = delta["delta"]
            row[f"delta_{metric}_lo"] = delta["lo"]
            row[f"delta_{metric}_hi"] = delta["hi"]
            row[f"{metric}_significant"] = delta["significant"]


def _write(out_dir: Path, rows: list[dict[str, Any]]) -> Path:
    lines = ["# 参数扫描结果", ""]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
    for group, items in groups.items():
        lines.append(f"## {group}")
        lines.append("")
        has_chunks = any("chunks" in item for item in items)
        header = (
            "| 变体 | 语料 | 方法 | recall@5 | MRR@5 | nDCG@5 | 重复率 "
            "| Δrecall [95%] | 显著 | ΔMRR [95%] | 显著 |"
        )
        separator = "|---|---|---|---|---|---|---|---|---|---|---|"
        if has_chunks:
            header += " 块数 |"
            separator += "---|"
        lines += [header, separator]
        for item in items:
            line = (
                f"| {item['name']} | {item['corpus']} | {item['method']} "
                f"| {item['recall_at_k']:.3f} | {item['mrr_at_k']:.3f} "
                f"| {item['ndcg_at_k']:.3f} | {item['duplicate_rate']:.3f} "
                f"| {item.get('delta_recall', 0.0):+.3f} "
                f"[{item.get('delta_recall_lo', 0.0):+.3f}, "
                f"{item.get('delta_recall_hi', 0.0):+.3f}] "
                f"| {'是' if item.get('recall_significant') else '否'} "
                f"| {item.get('delta_mrr', 0.0):+.3f} "
                f"[{item.get('delta_mrr_lo', 0.0):+.3f}, "
                f"{item.get('delta_mrr_hi', 0.0):+.3f}] "
                f"| {'是' if item.get('mrr_significant') else '否'} |"
            )
            if has_chunks:
                line += f" {item.get('chunks', '')} |"
            lines.append(line)
        lines.append("")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "sweep.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "sweep.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def main() -> int:
    args = _parse_args()
    groups = set(args.group) if args.group else None
    rows = _run_retrieval_variants(Path(args.data), groups, args.real)
    rows += _run_chunking_variants(groups, args.real)
    path = _write(Path(args.out), rows)
    print(f"sweep: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
