"""Evaluate retrieval over the real-project benchmark dataset.

Runs the production retrieval paths against the dataset built by `build.py`,
compares method families and parameter choices, probes the open design
questions (mining side-table cost, score scale, query shaping, material
budget units, confidence policy) and checks the versioned gates. Every corpus
is a fixture id such as `httpx/docs`, so its judgments, floors and report rows
never depend on a hard-coded pair of corpora.

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
    parser.add_argument(
        "--suggest-gates",
        action="store_true",
        help="print floors derived from this run's held-out measurements",
    )
    return parser.parse_args()


_REPORT_IDENTITY_KEYS = frozenset(
    {
        "kind",
        "project",
        "library",
        "version",
        "ecosystem",
        "revision",
        "source",
        "acquired_at",
        "payload_sha256",
        "urls_sha256",
        "commit",
        "library_id",
        "version_id",
        "subdir",
        "doc_version",
        "doc_version_signal",
    }
)


def _revision_line(manifest: dict[str, Any]) -> str:
    parts = [
        f"{name}@{info.get('revision')}" for name, info in manifest["fixtures"].items()
    ]
    return ", ".join(parts)


def _url_for(info: dict[str, Any]):
    """Judgment anchor for one corpus: absolute URL for documents, repo-relative for source.

    The source prefix mirrors the production chunk URL scheme
    (`repo://{ecosystem}/{library}/{version}/...`) and is derived from the
    fixture entry, so adding a library never touches this module.
    """
    if info["kind"] == "docs":
        return lambda url: url
    prefix = f"repo://{info['ecosystem']}/{info['library']}/{info['version']}/"
    return lambda rel: prefix + rel


def _corpus_urls(conn, scope: dict[str, Any]) -> set[str]:
    """Every page identity the dataset holds for one fixture, documents and sources alike."""
    rows = conn.execute(
        "SELECT DISTINCT pages.url FROM pages"
        " JOIN versions ON versions.id = pages.version_id"
        " JOIN libraries ON libraries.id = versions.library_id"
        " WHERE libraries.ecosystem = ? AND libraries.name = ?"
        " AND versions.version = ?",
        (scope["ecosystem"], scope["library"], scope["version"]),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _all_urls(conn) -> set[str]:
    """Every page identity the dataset holds, across all fixtures."""
    rows = conn.execute("SELECT DISTINCT url FROM documents").fetchall()
    return {str(row[0]) for row in rows}


def _validate_targets(
    name: str,
    conn,
    scope: dict[str, Any],
    judgments: list[dict[str, Any]],
    url_of,
    all_urls: set[str] | None = None,
) -> None:
    """Reject judgments anchored outside the corpus before they are scored.

    A target that the dataset never received is an authoring error, not a
    retrieval miss: it would quietly read as a failure of every method. A global
    judgment names complete URLs (it can land in any library), so its targets
    are checked against the whole dataset instead of the fixture scope.
    """
    valid = _corpus_urls(conn, scope)
    broken: dict[str, list[str]] = {}
    for judgment in judgments:
        if judgment.get("scope") == "global":
            pool = all_urls if all_urls is not None else valid
            missing_targets = [
                target for target in judgment.get("targets", []) if target not in pool
            ]
        else:
            missing_targets = [
                target
                for target in judgment.get("targets", [])
                if url_of(target) not in valid
            ]
        if missing_targets:
            broken[str(judgment["id"])] = missing_targets
    if broken:
        raise SystemExit(
            f"fixture {name}: judgment targets outside the corpus: {broken}"
        )


def _query_lang(entry: dict[str, Any]) -> str:
    """Query language, explicit on the entry or derived from its script.

    A cross-lingual judgment is a language pair, not a query type: the corpus
    side comes from the fixture declaration, so the entry only states its own.
    """
    lang = str(entry.get("query_lang") or "")
    if lang in ("zh", "en"):
        return lang
    if entry.get("query_lang") is not None:
        raise SystemExit(
            f"{entry.get('id')}: query_lang must be zh or en, got {lang!r}"
        )
    from libref.text.cjk import CJK_RUN_RE

    return "zh" if CJK_RUN_RE.search(str(entry.get("query", ""))) else "en"


def _load(judgments: Path = JUDGMENTS) -> dict:
    """Load the judgment set and enforce the tuning/holdout contract.

    Tuning reads dev, gates read test: a set where either side is missing cannot
    certify anything, so the file is rejected rather than silently halving.
    A judgment optionally scopes itself globally (retrieved without a library
    scope, for cross-library disambiguation) and always knows its own language.
    """
    data = json.loads(judgments.read_text(encoding="utf-8"))
    for corpus, entries in data.items():
        splits: dict[str, set[str]] = {}
        for entry in entries:
            split = entry.get("split")
            if split not in ("dev", "test"):
                raise SystemExit(f"{corpus}/{entry.get('id')} has no dev/test split")
            scope = entry.get("scope", "scoped")
            if scope not in ("scoped", "global"):
                raise SystemExit(
                    f"{corpus}/{entry.get('id')}: scope must be scoped or global"
                )
            entry["query_lang"] = _query_lang(entry)
            splits.setdefault(str(entry["type"]), set()).add(split)
        if set().union(*splits.values()) != {"dev", "test"}:
            raise SystemExit(f"{corpus} needs queries on both sides of the split")
        for query_type, seen in splits.items():
            if "test" not in seen:
                raise SystemExit(f"{corpus}/{query_type} has no held-out query")
    return data


def _gates(path: Path = GATES) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _corpus_languages() -> dict[str, str]:
    """Corpus language per fixture, from the spec.

    A `cjk` query over an English corpus and a latin query over a Chinese corpus
    fail for opposite reasons; the report and the gates have to say which side of
    the crossing a row belongs to.
    """
    from tests.e2e.bench import archive as bench_archive

    return {
        bench_archive.fixture_id(entry): str(entry.get("corpus_lang") or "en")
        for entry in bench_archive.select([])
    }


def _suggested_floor(value: float, queries: int) -> float:
    """A floor two queries below the held-out measurement, never tighter.

    One query moves a small set by far more than a real regression would, so the
    slack is measured in queries rather than in points; small sets get the wider
    slack they need to not flag noise.
    """
    slack = max(0.05, 2.0 / max(1, queries))
    return round(max(0.0, float(value) - slack), 2)


def _print_suggested_gates(summary: dict[str, Any], revisions: dict[str, str]) -> None:
    """Print floors derived from this run so a human can freeze them deliberately.

    Nothing here writes the gate file: freezing is a decision, and a floor that
    rewrites itself on every run stops being a gate.
    """
    floors: dict[str, Any] = {}
    absent: dict[str, float] = {}
    for corpus, methods in summary.items():
        if not isinstance(methods, dict):
            continue
        test = methods["precise"]["test"]
        queries = int(test["queries"])
        floors[corpus] = {
            "recall_at_k": _suggested_floor(test["recall_at_k"], queries),
            "mrr_at_k": _suggested_floor(test["mrr_at_k"], queries),
        }
        # The floor holds for every method family: the weakest lane sets it, so a
        # fused run cannot fail a bound that was only measured on full text.
        weakest = min(
            float(payload["absent"]["test_abstain_rate"])
            for payload in methods.values()
        )
        absent[corpus] = round(max(0.0, weakest - 0.15), 2)
    print(
        json.dumps(
            {
                "revisions": revisions,
                "precise_floors": floors,
                "absent_abstain_floors": absent,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


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


def _grade(url: str, wanted: set[str], heading: str | None, path: str) -> int:
    """2 for the target section the query names, 1 for the target page, 0 for neither."""
    if url not in wanted:
        return 0
    if heading and heading not in path:
        return 1
    return 2


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


def _pool_urls(conn, ids: list[int]) -> set[str]:
    """URLs of the candidate pool behind one result set, for pool-level diagnosis."""
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT DISTINCT url FROM documents WHERE id IN ({marks})", ids
    ).fetchall()
    return {str(row[0]) for row in rows}


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
    corpus_lang: str = "en",
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
            # A global judgment is retrieved without a library scope: it asks
            # whether the pipeline finds the right library, not the right page.
            effective = {} if judgment.get("scope") == "global" else scope
            try:
                results, status = await service.search_with_mode(
                    judgment["query"],
                    preset,
                    **effective,
                    limit=limit,
                )
            except ValueError as exc:
                raise SystemExit(f"query '{judgment['id']}' rejected: {exc}") from exc
            rank = 0
            heading = judgment.get("heading")
            # Global judgments name complete URLs; scoped ones name fixture-relative
            # targets resolved through the corpus address scheme.
            if judgment.get("scope") == "global":
                wanted = set(judgment.get("targets", []))
            else:
                wanted = {url_of(target) for target in judgment.get("targets", [])}
            gains = [
                _grade(
                    str(item.get("url") or ""),
                    wanted,
                    heading,
                    str(item.get("path") or ""),
                )
                for item in results
            ]
            for index, grade in enumerate(gains):
                if grade > 0:
                    rank = index + 1
                    break
            pool = _pool_urls(
                conn, [int(item) for item in (status.get("fused_order") or [])]
            )
            rows.setdefault(str(judgment["type"]), []).append(
                {
                    "id": judgment["id"],
                    "type": str(judgment["type"]),
                    "split": str(judgment["split"]),
                    "scope": str(judgment.get("scope", "scoped")),
                    "query_lang": str(judgment.get("query_lang") or "en"),
                    "rank": rank,
                    "gains": gains,
                    "best": 0 if not wanted else (2 if heading else 1),
                    "count": len(results),
                    "duplicates": _duplicate_count(results),
                    "scores": [float(item.get("score") or 0.0) for item in results],
                    "evidence": str(status["evidence"]),
                    "answerability": float(status["answerability"]),
                    "cross_lingual": bool(status.get("cross_lingual", False)),
                    "in_pool": bool(wanted and wanted.intersection(pool)),
                    "corpus_lang": corpus_lang,
                }
            )
        flat = [row for group in rows.values() for row in group]
        out[method] = {"rows": flat, "by_type": rows}
    return out


def _aggregate(evaluation: dict[str, Any], k: int) -> dict[str, Any]:
    from tests.e2e.bench.metrics import bootstrap_ci, metric_series, summarize

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
        absent = payload["by_type"].get("absent", [])
        test = [row for row in answerable if row["split"] == "test"]
        # A flagged downgrade is the pipeline behaving correctly, not a miss:
        # the in-scope lane is what the full-text floors gate on.
        in_scope = [row for row in test if not row.get("cross_lingual", False)]
        summary[method] = {
            "all": summarize(answerable, k),
            "dev": summarize([row for row in answerable if row["split"] == "dev"], k),
            "test": summarize(test, k),
            "test_in_scope": summarize(in_scope, k),
            "by_type": {
                name: summarize(group, k)
                for name, group in payload["by_type"].items()
                if name != "absent"
            },
            "by_direction": _direction_rates(answerable, k),
            "interval": {
                name: bootstrap_ci(values)
                for name, values in metric_series(test, k).items()
            },
            "absent": _absent_rates(absent),
            "wrong_abstain": _wrong_abstain(payload),
        }
    return summary


def _direction_rates(rows: list[dict[str, Any]], k: int) -> dict[str, dict[str, Any]]:
    """Metrics per query-language to corpus-language direction.

    Two crossings fail for opposite reasons (no shared vocabulary versus
    segmentation and expansion), so they share neither a row nor, under real
    models, a floor.
    """
    from tests.e2e.bench.metrics import summarize

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        corpus = "zh" if str(row.get("corpus_lang") or "en") == "cjk" else "en"
        grouped.setdefault(f"{row.get('query_lang', 'en')}->{corpus}", []).append(row)
    return {name: summarize(group, k) for name, group in grouped.items()}


def _absent_rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Two signals kept apart: did anything come back, and did the pipeline admit it."""

    def _rate(group: list[dict[str, Any]], predicate) -> float:
        if not group:
            return 0.0
        return sum(1 for row in group if predicate(row)) / len(group)

    return {
        "queries": len(rows),
        "empty_rate": _rate(rows, lambda row: row.get("count", 0) == 0),
        "abstain_rate": _rate(rows, lambda row: row["evidence"] == "none"),
        "test_queries": sum(1 for row in rows if row["split"] == "test"),
        "test_empty_rate": _rate(
            [row for row in rows if row["split"] == "test"],
            lambda row: row.get("count", 0) == 0,
        ),
        "test_abstain_rate": _rate(
            [row for row in rows if row["split"] == "test"],
            lambda row: row["evidence"] == "none",
        ),
    }


def _wrong_abstain(payload: dict[str, Any]) -> dict[str, float]:
    """Share of answerable queries refused although candidates were returned.

    A query that retrieved nothing is not a refusal: abstaining there is the only
    correct answer, and counting it would reward returning noise.
    """
    rows = [
        row
        for name, group in payload["by_type"].items()
        if name != "absent"
        for row in group
        if row.get("count", 0) > 0
    ]

    def _rate(group: list[dict[str, Any]]) -> float:
        if not group:
            return 0.0
        return sum(1 for row in group if row["evidence"] == "none") / len(group)

    return {
        "all": _rate(rows),
        "test": _rate([row for row in rows if row["split"] == "test"]),
    }


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
    conn,
    settings,
    judgments: list[dict[str, Any]],
    scope: dict[str, Any],
    limit: int,
    *,
    url_of,
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
            f" WHERE documents_fts MATCH ? AND EXISTS (SELECT 1 FROM documents d"
            " WHERE d.id = documents_fts.rowid AND d.version_id=?)"
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
        wanted_set = {url_of(target) for target in judgment.get("targets", [])}
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
    conn,
    settings,
    judgments: list[dict[str, Any]],
    scope: dict[str, Any],
    *,
    url_of,
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
        wanted_set = {url_of(target) for target in judgment.get("targets", [])}
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


def _classify_pool(rank: int, in_pool: bool) -> str:
    """Where a miss happened: the answer never entered the pool, or pooling
    dropped it before the top k. The choice of aggregation only matters for the
    second bucket."""
    if rank > 0:
        return "kept"
    return "aggregation_loss" if in_pool else "recall_loss"


def _pool_buckets(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Pool diagnosis over collected rows: no re-search, just the classification."""
    buckets = {"kept": 0, "aggregation_loss": 0, "recall_loss": 0}
    for row in rows:
        buckets[_classify_pool(int(row.get("rank", 0)), bool(row.get("in_pool")))] += 1
    return buckets


def _probe_fusion_rank_loss(
    conn,
    settings,
    judgments: list[dict[str, Any]],
    scope: dict[str, Any],
    limit: int,
    *,
    embedder,
    vectors,
    url_of=None,
) -> dict[str, Any]:
    """Three-way pool diagnosis: the target is kept in the top k, sits in the
    fusion pool but outside the top k (aggregation loss), or never entered the
    pool at all (recall loss)."""
    services = _services(conn, settings, embedder=embedder, vectors=vectors)
    rows: list[dict[str, Any]] = []
    for judgment in judgments:
        if judgment.get("type") == "absent":
            continue
        effective = {} if judgment.get("scope") == "global" else scope
        precise, _ = asyncio.run(
            services["precise"].search_with_mode(
                judgment["query"], "precise", **effective, limit=limit
            )
        )
        fused, fused_status = asyncio.run(
            services["fused"].search_with_mode(
                judgment["query"], "balanced", **effective, limit=limit
            )
        )
        if not precise:
            continue
        top_url = str(precise[0].get("url") or "")
        kept = any(str(item.get("url") or "") == top_url for item in fused)
        wanted: set[str] = set()
        if url_of is not None:
            wanted = {url_of(target) for target in judgment.get("targets", [])}
        pool = _pool_urls(
            conn, [int(item) for item in (fused_status.get("fused_order") or [])]
        )
        fused_urls = [str(item.get("url") or "") for item in fused]
        rank = 0
        for index, url in enumerate(fused_urls):
            if url in wanted:
                rank = index + 1
                break
        rows.append(
            {
                "id": judgment["id"],
                "type": judgment["type"],
                "kept": kept,
                "bucket": _classify_pool(rank, bool(wanted and wanted & pool)),
            }
        )
    total = len(rows)
    lost = [row["id"] for row in rows if not row["kept"]]
    buckets: dict[str, int] = {"kept": 0, "aggregation_loss": 0, "recall_loss": 0}
    by_type: dict[str, list[int]] = {}
    for row in rows:
        buckets[row["bucket"]] = buckets.get(row["bucket"], 0) + 1
        bucket = by_type.setdefault(str(row["type"]), [0, 0])
        bucket[0] += 1
        bucket[1] += 0 if row["kept"] else 1
    return {
        "queries": total,
        "top1_lost": len(lost),
        "lost_ids": lost,
        "aggregation_loss": buckets["aggregation_loss"],
        "recall_loss": buckets["recall_loss"],
        "by_type": {
            name: {"queries": counts[0], "lost": counts[1]}
            for name, counts in by_type.items()
        },
    }


def _check_gates(
    summary: dict[str, Any],
    gates: dict[str, Any],
    *,
    real: bool,
    revisions: dict[str, str],
    pool: set[str] | None = None,
) -> list[str]:
    """Gate the full-text path always, and the model paths only under real models.

    Every floor reads the held-out half only: the same queries that tuned a
    threshold cannot also certify it. The full-text path is production-
    representative in both modes. Fused rows only get absolute floors under
    `--real`: with the lexical stand-ins their quality reflects the stand-in, not
    the retrieval design. Every floor is carried per fixture and bound to the
    archive revision it was measured on, so a re-acquired corpus cannot silently
    keep an old floor. Pool fixtures supply material but carry no judgments, so
    they are never gated.

    Abstention is gated twice on purpose: a floor on refusing unanswerable
    queries, and a ceiling on refusing answerable ones. Cross-lingual floors only
    apply under real models: the stand-ins share no vocabulary across scripts.
    """
    failures: list[str] = []
    pool = pool or set()
    invariants = gates["invariants"]
    max_duplicate = float(invariants["max_duplicate_rate"])
    max_wrong_abstain = float(invariants["max_wrong_abstain_rate"])
    for corpus, methods in summary.items():
        if not isinstance(methods, dict):
            continue
        if corpus in pool:
            continue
        recorded = gates["revisions"].get(corpus)
        if recorded is None:
            failures.append(f"{corpus}: no gate revision recorded")
        elif recorded != revisions.get(corpus):
            failures.append(
                f"{corpus}: dataset revision {revisions.get(corpus)} differs from the "
                f"gate revision {recorded}; re-measure the floors"
            )
        # Downgraded cross-lingual rows are correct behavior, not misses: the
        # full-text floors gate the in-scope lane.
        scoped = methods.get("precise", {}).get("test_in_scope", {})
        precise = scoped or methods.get("precise", {}).get("test", {})
        floors = gates["precise_floors"].get(corpus)
        if floors is None:
            failures.append(f"{corpus}: no full-text floor recorded")
            floors = {}
        if real:
            for method, metrics in gates["real_floors"].get(corpus, {}).items():
                actual = methods.get(method, {}).get("test", {})
                for metric, floor in metrics.items():
                    if actual.get(metric, 0.0) < float(floor):
                        failures.append(
                            f"{corpus}/{method}: {metric}={actual.get(metric, 0.0):.3f}"
                            f" below floor {float(floor):.3f}"
                        )
            for method, metrics in (
                gates.get("cross_lingual_floors", {}).get(corpus, {}).items()
            ):
                directions = methods.get(method, {}).get("by_direction", {})
                for direction, expected in metrics.items():
                    actual = directions.get(direction, {})
                    for metric, floor in expected.items():
                        if actual.get(metric, 0.0) < float(floor):
                            failures.append(
                                f"{corpus}/{method}/{direction}: {metric}="
                                f"{actual.get(metric, 0.0):.3f} below floor "
                                f"{float(floor):.3f}"
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
            wrong = float(payload.get("wrong_abstain", {}).get("test", 0.0))
            if wrong > max_wrong_abstain:
                failures.append(
                    f"{corpus}/{method}: wrong_abstain_rate={wrong:.3f}"
                    f" exceeds {max_wrong_abstain:.3f}"
                )
        abstain_floor = gates["absent_abstain_floors"].get(corpus)
        if abstain_floor is None:
            failures.append(f"{corpus}: no abstain floor recorded")
            continue
        for method, payload in methods.items():
            actual = float(payload.get("absent", {}).get("test_abstain_rate", 0.0))
            if actual < float(abstain_floor):
                failures.append(
                    f"{corpus}/{method}: absent_abstain_rate={actual:.3f}"
                    f" below floor {float(abstain_floor):.3f}"
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
    languages: dict[str, str] | None = None,
) -> Path:
    lines: list[str] = ["# 检索问答基准报告（真实项目夹具）", ""]
    if models:
        lines.append(f"- {models}")
    lines.append(f"- 归档修订：{_revision_line(manifest)}")
    langs = languages or {}
    for name, info in manifest.get("fixtures", {}).items():
        parts = [
            f"{key}={info.get(key)}"
            for key in ("library", "ecosystem", "version", "revision", "source")
        ]
        if name in langs:
            parts.append(f"语料语言={langs[name]}")
        if info.get("doc_version"):
            parts.append(
                f"文档版本={info['doc_version']}（{info.get('doc_version_signal')}）"
            )
        if info.get("commit"):
            parts.append(f"提交={info['commit']}")
        identity = " / ".join(parts)
        counts = ", ".join(
            f"{key}={value}"
            for key, value in info.items()
            if key not in _REPORT_IDENTITY_KEYS and key != "page_paths"
        )
        tier = str(info.get("tier", "main"))
        suffix = "（干扰池：只供材料，不评分、不进门禁）" if tier == "pool" else ""
        lines.append(f"- {name}: {identity}；{counts}{suffix}")
    lines += [
        "",
        f"## 方法矩阵（Recall/MRR/nDCG @{k}，全部查询）",
        "",
        "区间为按查询重采样的 95% 置信区间；门禁读的是下面 test 分栏，不是这一栏。",
        "",
    ]
    for corpus, methods in summary.items():
        if not isinstance(methods, dict):
            continue
        lines += [f"### {corpus}", ""]
        lines.append("| 方法 | recall@k [95%] | MRR@k | nDCG@k | 空结果率 | 重复率 |")
        lines.append("|---|---|---|---|---|---|")
        for method, payload in methods.items():
            all_metrics = payload.get("all", {})
            recall_ci = payload.get("interval", {}).get("recall", {})
            lines.append(
                f"| {method} "
                f"| {all_metrics.get('recall_at_k', 0):.3f} "
                f"[{recall_ci.get('lo', 0):.3f}, {recall_ci.get('hi', 0):.3f}] "
                f"| {all_metrics.get('mrr_at_k', 0):.3f} "
                f"| {all_metrics.get('ndcg_at_k', 0):.3f} "
                f"| {all_metrics.get('empty_rate', 0):.3f} "
                f"| {all_metrics.get('duplicate_rate', 0):.3f} |"
            )
        lines.append("")
        lines.append("| 方法 | 划分 | 查询数 | recall@k | MRR@k | nDCG@k |")
        lines.append("|---|---|---|---|---|---|")
        for method, payload in methods.items():
            for split in ("dev", "all", "test"):
                metrics = payload.get(split, {})
                lines.append(
                    f"| {method} | {split} | {metrics.get('queries', 0)} "
                    f"| {metrics.get('recall_at_k', 0):.3f} "
                    f"| {metrics.get('mrr_at_k', 0):.3f} "
                    f"| {metrics.get('ndcg_at_k', 0):.3f} |"
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
        lines.append("| 方法 | 语言方向 | 查询数 | recall@k | MRR@k |")
        lines.append("|---|---|---|---|---|")
        for method, payload in methods.items():
            for direction, metrics in payload.get("by_direction", {}).items():
                lines.append(
                    f"| {method} | {direction} "
                    f"| {metrics.get('queries', 0)} "
                    f"| {metrics.get('recall_at_k', 0):.3f} "
                    f"| {metrics.get('mrr_at_k', 0):.3f} |"
                )
        lines.append("")
        lines.append(
            "全文路门禁读的是 test 分栏里未被跨语言降级的查询"
            "（`test_in_scope`），降级行不计入召回失败。"
        )
        absent_rows = [
            (
                f"| {method} | {payload['absent'].get('queries', 0)} "
                f"| {payload['absent'].get('empty_rate', 0):.3f} "
                f"| {payload['absent'].get('abstain_rate', 0):.3f} "
                f"| {payload['absent'].get('test_empty_rate', 0):.3f} "
                f"| {payload['absent'].get('test_abstain_rate', 0):.3f} "
                f"| {payload['wrong_abstain'].get('test', 0):.3f} |"
            )
            for method, payload in methods.items()
        ]
        lines += [
            "",
            "| 方法 | 无答案查询数 | 空结果率 | 弃答率 | test 空结果率 | test 弃答率 | 误弃答率 |",
            "|---|---|---|---|---|---|---|",
            *absent_rows,
        ]
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
            f"- 语料：`{side.get('fixture')}`；"
            f"存储规模 seq_patterns={side.get('seq_patterns')} 行，"
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
            f"- 语料 `{loss.get('fixture')}`：第一位页面在融合前 {k} 条内保留 "
            f"{loss.get('queries', 0) - loss.get('top1_lost', 0)}"
            f"/{loss.get('queries', 0)}，"
            f"丢失 {loss.get('top1_lost', 0)} 条："
            f"{', '.join(loss.get('lost_ids', [])) or '无'}"
        ),
        (
            f"- 池级三分：目标在 top{k} 内 {loss.get('queries', 0) - loss.get('aggregation_loss', 0) - loss.get('recall_loss', 0)} 条、"
            f"在池内但掉出 top{k}（聚合丢分）{loss.get('aggregation_loss', 0)} 条、"
            f"不在池内（召回丢分）{loss.get('recall_loss', 0)} 条"
        ),
    ]
    diagnosis = probes.get("pool_diagnosis", {})
    if diagnosis:
        lines += [
            "",
            f"各夹具池级三分（fused，top{k}）：",
            "",
            "| 夹具 | 命中 | 聚合丢分 | 召回丢分 |",
            "|---|---|---|---|",
        ]
        for corpus, buckets in diagnosis.items():
            lines.append(
                f"| {corpus} | {buckets.get('kept', 0)} "
                f"| {buckets.get('aggregation_loss', 0)} "
                f"| {buckets.get('recall_loss', 0)} |"
            )
    for name, counts in loss.get("by_type", {}).items():
        lines.append(f"- {name}: 丢失 {counts.get('lost')}/{counts.get('queries')}")
    shaping = probes.get("query_shaping", {})
    lines += [
        "",
        "### 查询整形变体",
        "",
        f"语料 `{shaping.get('fixture')}`。",
        "",
        "| 变体 | recall@k | MRR@k |",
        "|---|---|---|",
    ]
    for name, rows in shaping.items():
        if name == "fixture":
            continue
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
            f"- 语料 `{units.get('fixture')}`；配置：单材料 "
            f"{units.get('material_limit')} 字符，总预算 "
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
            f"- 语料 `{confidence.get('fixture')}`；回放用例 "
            f"{confidence.get('cases')} 个；"
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
    suggest: bool = False,
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
    judgments = _load()
    gates = _gates()
    languages = _corpus_languages()
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
    fixtures = manifest["fixtures"]
    pool = {name for name, info in fixtures.items() if info.get("tier") == "pool"}
    scored = {name: info for name, info in fixtures.items() if name not in pool}
    for name in pool:
        if name in judgments:
            raise SystemExit(f"fixture {name}: pool fixtures carry no judgments")
    for name in scored:
        if name not in judgments:
            raise SystemExit(f"fixture {name}: manifest has no judgment set")
    scopes = {
        name: {
            "library": info["library"],
            "version": info["version"],
            "ecosystem": info["ecosystem"],
        }
        for name, info in scored.items()
    }
    urls = {name: _url_for(info) for name, info in scored.items()}
    for name in scored:
        _validate_targets(
            name, conn, scopes[name], judgments[name], urls[name], _all_urls(conn)
        )
    try:
        evaluations = {
            name: asyncio.run(
                _evaluate_corpus(
                    conn,
                    settings,
                    judgments[name],
                    urls[name],
                    scopes[name],
                    limit,
                    embedder=embedder,
                    vectors=vectors,
                    rerank=rerank,
                    corpus_lang=languages.get(name, "en"),
                )
            )
            for name in scored
        }
        summary = {name: _aggregate(item, limit) for name, item in evaluations.items()}
        probes: dict[str, Any] = {}
        if probes_enabled:
            docs_id = _representative(scored, "docs")
            code_id = _representative(scored, "code")
            # Pool fixtures never reach the probes: they need a kind to run on.
            if docs_id in pool or code_id in pool:
                raise SystemExit("pool fixtures cannot serve as probe corpora")
            docs_scoped = [
                entry for entry in judgments[docs_id] if entry.get("scope") != "global"
            ]
            code_scoped = [
                entry for entry in judgments[code_id] if entry.get("scope") != "global"
            ]
            probes = {
                "chunk_identity": _probe_chunk_identity(conn),
                "side_tables": {
                    "fixture": code_id,
                    **_probe_side_tables(
                        conn,
                        settings,
                        code_scoped,
                        scopes[code_id],
                        embedder=embedder,
                        vectors=vectors,
                    ),
                },
                "score_scale": {
                    name: _probe_score_scale(item) for name, item in evaluations.items()
                },
                "pool_diagnosis": {
                    name: _pool_buckets(
                        [
                            row
                            for row in item["fused"]["rows"]
                            if row.get("type") != "absent"
                        ]
                    )
                    for name, item in evaluations.items()
                },
                "fusion_rank_loss": {
                    "fixture": docs_id,
                    **_probe_fusion_rank_loss(
                        conn,
                        settings,
                        judgments[docs_id],
                        scopes[docs_id],
                        limit,
                        embedder=embedder,
                        vectors=vectors,
                        url_of=urls[docs_id],
                    ),
                },
                "query_shaping": {
                    "fixture": docs_id,
                    **_probe_query_shaping(
                        conn,
                        settings,
                        docs_scoped,
                        scopes[docs_id],
                        limit,
                        url_of=urls[docs_id],
                    ),
                },
                "material_units": {
                    "fixture": docs_id,
                    **_probe_material_units(
                        conn, settings, docs_scoped, scopes[docs_id]
                    ),
                },
                "confidence": {
                    "fixture": docs_id,
                    **_probe_confidence(
                        conn,
                        settings,
                        docs_scoped,
                        scopes[docs_id],
                        url_of=urls[docs_id],
                    ),
                },
            }
        revisions = {name: str(info["revision"]) for name, info in fixtures.items()}
        if suggest:
            _print_suggested_gates(summary, revisions)
        failures = _check_gates(
            summary, gates, real=real, revisions=revisions, pool=pool
        )
        _report(
            manifest,
            summary,
            probes,
            failures,
            out_dir,
            limit,
            models=_model_line(settings, real),
            languages=languages,
        )
    finally:
        conn.close()
    return {"summary": summary, "probes": probes, "failures": failures}


def _representative(fixtures: dict[str, Any], kind: str) -> str:
    """First fixture of a kind in spec order: the one the probes are measured on.

    Probes are per-query diagnostics, so they run on a single representative
    corpus per kind instead of multiplying the run time by the number of
    projects; the report names the fixture they belong to.
    """
    for name, info in fixtures.items():
        if info["kind"] == kind:
            return name
    raise SystemExit(f"manifest has no {kind} fixture: {sorted(fixtures)}")


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
        suggest=args.suggest_gates,
    )
    if not args.gate_only:
        print(f"report: {Path(args.out) / 'report.md'}")
    for failure in result["failures"]:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
