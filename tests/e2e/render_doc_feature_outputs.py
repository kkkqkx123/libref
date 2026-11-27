"""Render document-feature review samples for manual inspection.

Usage: uv run python tests/e2e/render_doc_feature_outputs.py [--out-dir DIR]
Builds a deterministic seed database from versioned fixtures, runs the production
derivation and retrieval paths, then writes readable reports under tests/e2e/outputs.
No network, vector service, or language model is used.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parents[1]) not in sys.path:
    # Direct execution keeps the same import root as the module invocation.
    sys.path.insert(0, str(HERE.parents[1]))
FIXTURES = HERE / "fixtures" / "doc_features"
DRIFT_SAMPLES = FIXTURES / "drift_samples.json"
DEFAULT_OUT = HERE / "outputs" / "doc_features"
REPORT_NAMES = (
    "01-cases.txt",
    "02-blocks.txt",
    "03-links.txt",
    "04-coverage.txt",
    "05-consistency.txt",
    "06-questions.txt",
    "07-ranking.txt",
    "08-metrics.txt",
    "09-drift.txt",
    "10-drift-review.txt",
    "SUMMARY.txt",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="render document feature review outputs"
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


def load_cases() -> dict[str, Any]:
    """Shared fixture loader for the renderer and its review test."""
    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


def load_questions() -> dict[str, list[dict[str, Any]]]:
    return json.loads((FIXTURES / "questions.json").read_text(encoding="utf-8"))


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _drift_review_lines() -> tuple[str, dict[str, Any]]:
    """Human-labeled drift samples; the report observes, never gates."""
    samples = json.loads(DRIFT_SAMPLES.read_text(encoding="utf-8")).get("samples", [])
    auto = [row for row in samples if row.get("auto_verdict") == "drift"]
    false_positive = [row for row in auto if row.get("human_verdict") != "drift"]
    stats = {
        "samples": len(samples),
        "auto_drift": len(auto),
        "false_positive": len(false_positive),
        "false_positive_rate": (len(false_positive) / len(auto) if auto else 0.0),
        "measurable": bool(auto),
    }
    lines = [
        "# Human-reviewed drift samples",
        "",
        f"- samples: {stats['samples']}",
        f"- auto drift: {stats['auto_drift']}",
        f"- false positive: {stats['false_positive']}",
        f"- false positive rate: {stats['false_positive_rate']:.3f}",
        "",
    ]
    for row in samples:
        lines.append(
            f"- {row.get('corpus', '-')} {row.get('url', '-')} "
            f"auto={row.get('auto_verdict', '-')} "
            f"human={row.get('human_verdict', '-')} "
            f"reason={row.get('reason', '-')}"
        )
    if not samples:
        lines.append("(no human-labeled samples)")
    if not stats["measurable"]:
        lines.append("Not measurable until an automatic drift sample is labeled.")
    return "\n".join(lines), stats


def _write(out_dir: Path, name: str, body: str) -> None:
    (out_dir / name).write_text(body.rstrip() + "\n", encoding="utf-8")


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _seed_entities(
    conn, library_id: int, version_id: int, cases: dict
) -> dict[str, int]:
    out: dict[str, int] = {}
    for index, entity in enumerate(cases["entities"], start=1):
        name = str(entity["qualified_name"])
        conn.execute(
            "INSERT INTO api_entities(entity_uid, library_id, version_id, qualified_name,"
            " kind) VALUES(?,?,?,?,?)",
            (f"uid-{version_id}-{index}", library_id, version_id, name, entity["kind"]),
        )
        out[name] = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return out


def _seed_patterns(conn, version_id: int, cases: dict) -> None:
    from libref.services.code.usage_sources import resolve_profile

    profile = resolve_profile(conn, version_id=version_id, tenant_id="default")
    for row in cases["patterns"]:
        conn.execute(
            "INSERT INTO seq_patterns(library_id, version_id, profile_id, pattern, support,"
            " is_closed) VALUES(?,?,?,?,?,1)",
            (
                conn.execute(
                    "SELECT library_id FROM versions WHERE id=?", (version_id,)
                ).fetchone()[0],
                version_id,
                profile["id"],
                json.dumps(row["pattern"], ensure_ascii=False),
                int(row["support"]),
            ),
        )
    conn.commit()


def _seed_feature_nodes(conn, version_id: int, cases: dict) -> None:
    from libref.services.code.usage_sources import resolve_profile

    profile = resolve_profile(conn, version_id=version_id, tenant_id="default")
    library_id = conn.execute(
        "SELECT library_id FROM versions WHERE id=?", (version_id,)
    ).fetchone()[0]
    for row in cases.get("feature_nodes", []):
        conn.execute(
            "INSERT INTO feature_nodes(library_id, version_id, profile_id, path, parent,"
            " depth, kind, calls, scopes) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                library_id,
                version_id,
                profile["id"],
                str(row["path"]),
                str(row.get("parent") or ""),
                int(row["depth"]),
                str(row.get("kind") or "function"),
                int(row.get("calls", 0)),
                int(row.get("scopes", 0)),
            ),
        )
    conn.commit()


def _seed_version(
    conn,
    settings,
    pipeline,
    cases: dict,
    version: str,
    pages: list[dict],
    files: dict[str, str] | None = None,
) -> tuple[int, int, dict[str, int]]:
    """Store one page per fixture entry and register its code fences as doc snippets."""
    from libref.astminer.code_blocks import fenced_blocks
    from libref.crawler.code_blocks import html_code_blocks
    from libref.services.code.usage_ingest import doc_source_key, ingest_snippets
    from libref.services.registry.libraries import LibraryService
    from libref.store.document_store import DocumentInput, DocumentStore

    library_id, version_id = LibraryService(conn).ensure_library_version(
        str(cases["library"]), version, str(cases["ecosystem"])
    )
    _seed_entities(conn, library_id, version_id, cases)
    store = DocumentStore(conn)
    doc_ids: dict[str, int] = {}
    for page in pages:
        url = str(page["url"])
        rel = (files or {}).get(url, str(page["file"]))
        raw = (FIXTURES / rel).read_text(encoding="utf-8")
        if page["kind"] == "html":
            scraped = pipeline.scrape_cached(raw, url)
            markdown = scraped.markdown
            snippets = html_code_blocks(
                scraped.cleaned_html,
                sample_prefix=doc_source_key(version_id, url),
                url=url,
                heading=scraped.title or str(page["title"]),
            )
            origin = "html_code_block"
        else:
            markdown = raw
            snippets = fenced_blocks(
                markdown,
                sample_prefix=doc_source_key(version_id, url),
                url=url,
                heading=str(page["title"]),
            )
            origin = "markdown_fence"
        result = store.store_page(
            version_id,
            library_id,
            url,
            "stored",
            chunks=[
                DocumentInput(
                    url=url,
                    path=str(page["path"]),
                    title=str(page["title"]),
                    content=markdown,
                    sort_order=0,
                )
            ],
            raw_content=markdown,
        )
        doc_ids[url] = int(result.inserted_ids[0])
        if snippets:
            ingest_snippets(
                conn,
                str(cases["library"]),
                version,
                snippets,
                source_key=doc_source_key(version_id, url),
                source_role="doc_snippet",
                origin_kind=origin,
                ecosystem=str(cases["ecosystem"]),
                target_filter={"prefixes": [f"{cases['library']}."]},
                source_url=url,
                selection={"url": url, "heading": page["title"]},
                settings=settings,
            )
    _seed_patterns(conn, version_id, cases)
    _seed_feature_nodes(conn, version_id, cases)
    return library_id, version_id, doc_ids


def _case_lines(cases: dict) -> str:
    lines = [
        "Fixtures: tests/e2e/fixtures/doc_features",
        f"Generated: {_stamp()}",
        "",
        (
            f"Library: {cases['library']}  version: {cases['version']}"
            f"  next version: {cases['next_version']}"
        ),
        "",
        "Pages:",
    ]
    for page in cases["pages"]:
        lines.append(
            f"- {page['url']} | {page['title']} | {page['path']} | {page['file']}"
        )
    lines += ["", "Entities:"]
    lines += [
        f"- {item['qualified_name']} ({item['kind']})" for item in cases["entities"]
    ]
    lines += ["", "Feature nodes:"]
    lines += [
        f"- {item['path']} parent={item.get('parent', '') or '-'}"
        for item in cases.get("feature_nodes", [])
    ]
    lines += ["", "Patterns:"]
    lines += [
        f"- {' -> '.join(item['pattern'])} | support={item['support']}"
        for item in cases["patterns"]
    ]
    lines += ["", "Hand-computed expectations:"]
    lines += [f"- {key}: {value}" for key, value in cases["expect"].items()]
    return "\n".join(lines)


def _block_lines(insight, version_id: int, doc_ids: dict[str, int]) -> str:
    lines = [
        "# Document blocks: facet, heading path, fence ratio, deprecated marker",
        "",
    ]
    facets = insight.features(version_id, limit=1000)
    by_doc: dict[int, dict[str, Any]] = {}
    for row in facets:
        by_doc.setdefault(int(row["document_id"]), {})[str(row["facet"])] = row
    for url, doc_id in sorted(doc_ids.items(), key=lambda pair: pair[1]):
        values = by_doc.get(doc_id, {})
        lines.append(f"- {url} (document_id={doc_id})")
        for name in ("block_type", "heading_depth", "code_ratio", "has_table"):
            row = values.get(name)
            detail = row["details"].get("value") if row else None
            lines.append(f"  - {name}: {detail}")
        for name in (
            "example_language",
            "example_symbols",
            "param_names",
            "return_hint",
            "deprecated",
        ):
            row = values.get(name)
            detail = row["details"].get("value") if row else None
            lines.append(f"  - {name}: {_dump(detail)}")
    return "\n".join(lines)


def _link_lines(insight, version_id: int) -> tuple[str, list[dict[str, Any]]]:
    rows = insight.links(version_id, limit=1000)
    lines = ["# Alignment edges: match kind, evidence, confidence", ""]
    for row in rows:
        lines.append(
            f"- document_id={row['document_id']} api={row['api']} "
            f"kind={row['match_kind']} confidence={row['confidence']:.2f}"
        )
        lines.append(f"  nodes: {_dump(row.get('feature_nodes', []))}")
        lines.append(f"  evidence: {_dump(row['evidence'])}")
    if not rows:
        lines.append("(no links)")
    return "\n".join(lines), rows


def _coverage_lines(
    insight, version_id: int, library_id: int
) -> tuple[str, list[dict]]:
    rows = insight.coverage(version_id, library_id, limit=1000)
    lines = ["# Pattern coverage: hash, kind, support", ""]
    for row in rows:
        lines.append(
            f"- {row['pattern_hash'][:12]} {' -> '.join(row['pattern'])} "
            f"kind={row['covered_kind']} support={row['support']} "
            f"document_id={row['document_id']}"
        )
    if not rows:
        lines.append("(no coverage)")
    return "\n".join(lines), rows


def _consistency_lines(
    insight, version_id: int, library_id: int
) -> tuple[str, list[dict[str, Any]]]:
    rows = insight.consistency(version_id, library_id, limit=1000)
    lines = ["# Consistency verdicts and evidence fingerprints", ""]
    for row in rows:
        lines.append(
            f"- document_id={row['document_id']} facet={row['facet']} "
            f"verdict={row['verdict']} model={row['model'] or 'rule'} "
            f"hash={row['evidence_hash'][:12]} note={row['note'] or '-'}"
        )
    if not rows:
        lines.append("(no verdicts)")
    return "\n".join(lines), rows


def _resolve_anchor(conn, corpus: str, question: dict) -> list[str]:
    """Resolve stable anchors without database ids, line numbers, or chunk ordinals."""
    problems: list[str] = []
    target_urls = [str(item) for item in question.get("targets", [])]
    if not target_urls:
        problems.append("no targets")
    for url in target_urls:
        found = conn.execute(
            "SELECT 1 FROM documents WHERE url=? LIMIT 1", (url,)
        ).fetchone()
        if found is None:
            problems.append(f"url:{url}")
            continue
        heading = str(question.get("expected_facets", {}).get("heading") or "")
        if heading:
            paths = [
                str(row[0] or "")
                for row in conn.execute(
                    "SELECT DISTINCT json_extract(metadata, '$.path') FROM documents WHERE url=?",
                    (url,),
                ).fetchall()
            ]
            if not any(heading.lower() in path.lower() for path in paths):
                problems.append(f"heading:{heading}@{url}")
    for name in question.get("expected_entities", []):
        row = conn.execute(
            "SELECT 1 FROM api_entities WHERE qualified_name=? LIMIT 1", (str(name),)
        ).fetchone()
        if row is None:
            problems.append(f"entity:{name}")
    del corpus
    return problems


def _question_lines(
    conn, questions: dict[str, list[dict]], corpora: list[str]
) -> tuple[str, dict[str, list[str]]]:
    lines = ["# Judgment entries and stable anchor resolution", ""]
    unresolved: dict[str, list[str]] = {}
    for corpus in corpora:
        entries = questions.get(corpus, [])
        lines.append(f"## {corpus} ({len(entries)} entries)")
        problems: list[str] = []
        for question in entries:
            issues = _resolve_anchor(conn, corpus, question)
            if issues:
                problems.append(f"{question['id']}: {', '.join(issues)}")
            lines.append(
                f"- {question['id']} [{question['source']}/{question['split']}] "
                f"{question['query']}"
            )
            lines.append(
                f"  targets={_dump(question['targets'])} "
                f"entities={_dump(question['expected_entities'])}"
            )
            lines.append(
                f"  facets={_dump(question['expected_facets'])} "
                f"verdict={question['expected_verdict']} ref={question['source_ref']}"
            )
            if issues:
                lines.append(f"  UNRESOLVED: {_dump(issues)}")
        if problems:
            unresolved[corpus] = problems
            lines.append(f"Unresolved anchors ({len(problems)}):")
            lines += [f"  - {item}" for item in problems]
        else:
            lines.append("Unresolved anchors: none")
        lines.append("")
    return "\n".join(lines), unresolved


def _grade(item: dict, question: dict) -> int:
    targets = {str(url) for url in question.get("targets", [])}
    if str(item.get("url") or "") not in targets:
        return 0
    heading = str(question.get("expected_facets", {}).get("heading") or "")
    if heading and heading.lower() not in str(item.get("path") or "").lower():
        return 1
    grades = question.get("grades") or {}
    return int(grades.get("strong", 2))


def _filter_stats(conn, found: list[dict], question: dict) -> dict[str, Any]:
    """Observe how many hits survive each explicit document-derived filter."""
    from libref.store.retriever import filter_by_doc_facets

    targets = {str(url) for url in question.get("targets", [])}
    ids = [int(item["id"]) for item in found]
    url_by_id = {int(item["id"]): str(item.get("url") or "") for item in found}
    filters = {
        "examples_only": {"block_type": "example"},
        "exclude_deprecated": {"exclude_deprecated": True},
        "aligned_only": {"aligned_only": True},
    }
    out: dict[str, Any] = {}
    for name, options in filters.items():
        kept = filter_by_doc_facets(conn, ids, **options)
        out[name] = {
            "results": len(ids),
            "kept": len(kept),
            "hits": int(any(url_by_id[doc_id] in targets for doc_id in kept)),
        }
    return out


def _rank_questions(
    conn, settings, questions: list[dict], limit: int
) -> dict[str, Any]:
    from libref.services.docs.search import SearchService
    from tests.e2e.bench.fakes import BagOfTermsEmbedder, SqliteVecStore

    embedder = BagOfTermsEmbedder()
    vectors = SqliteVecStore(conn, embedder)
    import asyncio

    asyncio.run(vectors.prepare())
    off = SearchService(conn, settings, vec_store=vectors, embed=embedder)
    on_settings = settings.model_copy(update={"doc_feature_annotation_top_n": 5})
    on = SearchService(conn, on_settings, vec_store=vectors, embed=embedder)
    rows_off: list[dict] = []
    rows_on: list[dict] = []
    filter_rows: list[dict] = []
    lines = [
        "# Retrieval ranking with result annotation off and on",
        "",
        "Both columns share one pool depth and one chunk budget; only text annotation changes.",
        "",
    ]
    for question in questions:
        best = 2 if question.get("expected_facets", {}).get("heading") else 1
        results: dict[str, list[dict]] = {}
        per_query: dict[str, dict] = {}
        for label, service in (("off", off), ("on", on)):
            found, status = asyncio.run(
                service.search_with_mode(
                    question["query"], "balanced", library=None, limit=limit
                )
            )
            gains = [_grade(item, question) for item in found]
            results[label] = found
            per_query[label] = {
                "gains": gains,
                "best": best,
                "count": len(found),
                "duplicates": 0,
                "evidence": str(status.get("evidence") or ""),
            }
        ranks = {}
        for label in ("off", "on"):
            gains = per_query[label]["gains"]
            ranks[label] = next((i + 1 for i, g in enumerate(gains) if g > 0), 0)
        lines.append(
            f"- {question['id']} rank off={ranks['off']} on={ranks['on']} "
            f"best={best} evidence={per_query['on']['evidence']}"
        )
        for label in ("off", "on"):
            top = [
                f"{item.get('url')}#{item.get('path')}={g}"
                for item, g in zip(
                    results[label], per_query[label]["gains"], strict=False
                )
                if g > 0
            ]
            lines.append(f"  {label}: {_dump(top) if top else '(no target hit)'}")
        stats = _filter_stats(conn, results["off"], question)
        lines.append(f"  filters: {_dump(stats)}")
        rows_off.append(per_query["off"])
        rows_on.append(per_query["on"])
        filter_rows.append(stats)
    total = len(filter_rows)
    filters = {
        name: {
            "queries": total,
            "results": sum(row[name]["results"] for row in filter_rows),
            "kept": sum(row[name]["kept"] for row in filter_rows),
            "hits": sum(row[name]["hits"] for row in filter_rows),
            "hit_rate": (
                sum(row[name]["hits"] for row in filter_rows) / total if total else 0.0
            ),
        }
        for name in ("examples_only", "exclude_deprecated", "aligned_only")
    }
    return {"lines": lines, "off": rows_off, "on": rows_on, "filters": filters}


def _metrics(
    cases: dict,
    questions: list[dict],
    links: list[dict],
    coverage: list[dict],
    consistency: list[dict],
    features: list[dict],
    doc_ids: dict[str, int],
    ranking: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    from tests.e2e.bench.metrics import (
        metric_series,
        paired_delta,
        summarize,
    )

    url_by_doc = {doc_id: url for url, doc_id in doc_ids.items()}
    facet_values = {
        (int(row["document_id"]), str(row["facet"])): row["details"].get("value")
        for row in features
    }
    block_types = {
        doc_id: value
        for (doc_id, facet), value in facet_values.items()
        if facet == "block_type"
    }
    linked_entities: dict[int, set[str]] = {}
    for row in links:
        linked_entities.setdefault(int(row["document_id"]), set()).add(str(row["api"]))
    verdicts = {
        (int(row["document_id"]), str(row["facet"])): str(row["verdict"])
        for row in consistency
    }
    node_paths: dict[int, set[str]] = {}
    for row in links:
        node_paths.setdefault(int(row["document_id"]), set()).update(
            str(path) for path in row.get("feature_nodes") or []
        )
    alignment = []
    block_match = []
    facet_match = []
    drift_mismatch = []
    node_match = []
    for question in questions:
        targets = [str(url) for url in question.get("targets", [])]
        docs = [doc_ids[url] for url in targets if url in doc_ids]
        if not docs:
            continue
        have = set().union(*(linked_entities.get(doc, set()) for doc in docs))
        want = {str(name) for name in question.get("expected_entities", [])}
        alignment.append(want <= have)
        if "expected_feature_nodes" in question:
            have_nodes = set().union(*(node_paths.get(doc, set()) for doc in docs))
            want_nodes = {str(path) for path in question["expected_feature_nodes"]}
            node_match.append(have_nodes == want_nodes)
        expected_facets = {
            str(key): value
            for key, value in question.get("expected_facets", {}).items()
            if key != "heading"
        }
        if expected_facets:
            facet_match.append(
                all(
                    any(facet_values.get((doc, key)) == value for doc in docs)
                    for key, value in expected_facets.items()
                )
            )
        expected_type = expected_facets.get("block_type")
        if expected_type:
            block_match.append(
                any(block_types.get(doc) == expected_type for doc in docs)
            )
        expected_verdict = str(question.get("expected_verdict") or "")
        if expected_verdict:
            actual = [verdict for (doc, _), verdict in verdicts.items() if doc in docs]
            drift_mismatch.append(expected_verdict not in actual)
    covered = {" -> ".join(row["pattern"]) for row in coverage}
    expected_coverage = {" -> ".join(row["pattern"]) for row in cases["patterns"]}
    recall = len(expected_coverage & covered) / max(1, len(expected_coverage))
    k = 5
    before = summarize(ranking["off"], k)
    after = summarize(ranking["on"], k)
    deltas = {
        name: paired_delta(
            metric_series(ranking["on"], k)[name],
            metric_series(ranking["off"], k)[name],
        )
        for name in ("ndcg", "mrr")
    }
    values = {
        "questions": len(questions),
        "entity_alignment_accuracy": sum(alignment) / max(1, len(alignment)),
        "feature_node_accuracy": sum(node_match) / max(1, len(node_match)),
        "facet_consistency": sum(facet_match) / max(1, len(facet_match)),
        "block_type_consistency": sum(block_match) / max(1, len(block_match)),
        "coverage_recall": recall,
        "drift_mismatch_rate": sum(drift_mismatch) / max(1, len(drift_mismatch)),
        "ndcg_off": before["ndcg_at_k"],
        "ndcg_on": after["ndcg_at_k"],
        "mrr_off": before["mrr_at_k"],
        "mrr_on": after["mrr_at_k"],
        "ndcg_delta": deltas["ndcg"],
        "mrr_delta": deltas["mrr"],
        "filters": ranking["filters"],
    }
    lines = ["# Derived metrics (review observations, not unit assertions)", ""]
    for key, value in values.items():
        if isinstance(value, int):
            shown = str(value)
        elif isinstance(value, dict):
            shown = _dump(value)
        else:
            shown = f"{value:.3f}"
        lines.append(f"- {key}: {shown}")
    lines += ["", "Filter effectiveness:"]
    for name, stats in ranking["filters"].items():
        lines.append(
            f"- {name}: kept={stats['kept']}/{stats['results']} "
            f"hits={stats['hits']} hit_rate={stats['hit_rate']:.3f}"
        )
    lines.append("")
    lines.append(f"Covered patterns: {_dump(sorted(covered))}")
    lines.append(f"Expected patterns: {_dump(sorted(expected_coverage))}")
    lines.append(f"Indexed documents: {_dump(sorted(url_by_doc.values()))}")
    return "\n".join(lines), values


def _invariants(
    cases: dict,
    questions: dict,
    unresolved: dict,
    links: list,
    coverage: list,
    consistency: list,
    pages: list,
    values: dict,
) -> dict[str, bool]:
    want = cases["expect"]
    observed_nodes = {
        str(path) for row in links for path in row.get("feature_nodes") or []
    }
    return {
        "page_count": len(pages) == int(want["pages"]),
        "links_present": len(links) >= int(want["links_at_least"]),
        "feature_nodes_present": sum(
            len(row.get("feature_nodes") or []) for row in links
        )
        >= int(want.get("feature_nodes_at_least", 0)),
        "feature_node_paths_present": set(want.get("feature_node_paths", [])).issubset(
            observed_nodes
        ),
        "coverage_present": any(
            name in {" -> ".join(row["pattern"]) for row in coverage}
            for name in want["covered_patterns"]
        ),
        "verdicts_present": set(want["verdicts"]).issubset(
            {str(row["verdict"]) for row in consistency}
        ),
        "anchors_resolved": not any(unresolved.values()),
        "judgments_loaded": all(questions.get(key) for key in questions),
        "metrics_computed": values["questions"] > 0,
    }


def render(out_dir: Path) -> dict[str, Any]:
    """Build the fixture database and write all review reports."""
    from libref.db import apply_migrations, open_db
    from libref.services.docs.doc_insight import DocInsightService
    from libref.services.docs.feature_refresh import refresh_doc_features
    from libref.services.docs.page_pipeline import PagePipeline
    from tests.e2e.bench.fakes import bench_settings

    cases = load_cases()
    questions = load_questions()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="libref-doc-features-") as tmp:
        db_path = Path(tmp) / "doc_features.db"
        conn = open_db(db_path)
        apply_migrations(conn)
        try:
            settings = bench_settings(
                db_path,
                doc_feature_annotation_top_n=0,
                freq_alpha=0.0,
                facet_alpha=0.0,
            )
            pipeline = PagePipeline(settings)
            _library_id, version_id, doc_ids = _seed_version(
                conn, settings, pipeline, cases, str(cases["version"]), cases["pages"]
            )
            counts = refresh_doc_features(
                conn,
                str(cases["library"]),
                str(cases["version"]),
                ecosystem=str(cases["ecosystem"]),
                settings=settings,
            )
            insight = DocInsightService(conn, settings)
            features = insight.features(version_id, limit=1000)
            _link_text, links = _link_lines(insight, version_id)
            _coverage_text, coverage = _coverage_lines(insight, version_id, _library_id)
            _consistency_text, consistency = _consistency_lines(
                insight, version_id, _library_id
            )
            _question_text, unresolved = _question_lines(conn, questions, ["demo"])
            demo_questions = questions.get("demo", [])
            ranking = _rank_questions(conn, settings, demo_questions, limit=5)
            _metric_text, values = _metrics(
                cases,
                demo_questions,
                links,
                coverage,
                consistency,
                features,
                doc_ids,
                ranking,
            )
            next_pages = [
                dict(page, file=cases["next_pages"].get(str(page["url"]), page["file"]))
                for page in cases["pages"]
            ]
            _seed_version(
                conn,
                settings,
                pipeline,
                cases,
                str(cases["next_version"]),
                next_pages,
                cases["next_pages"],
            )
            refresh_doc_features(
                conn,
                str(cases["library"]),
                str(cases["next_version"]),
                ecosystem=str(cases["ecosystem"]),
                settings=settings,
            )
            drift = insight.compare_versions(
                str(cases["library"]),
                str(cases["version"]),
                str(cases["next_version"]),
                ecosystem=str(cases["ecosystem"]),
            )
            checks = _invariants(
                cases,
                questions,
                unresolved,
                links,
                coverage,
                consistency,
                cases["pages"],
                values,
            )
            _write(out_dir, "01-cases.txt", _case_lines(cases))
            _write(out_dir, "02-blocks.txt", _block_lines(insight, version_id, doc_ids))
            _write(out_dir, "03-links.txt", _link_text)
            _write(out_dir, "04-coverage.txt", _coverage_text)
            _write(out_dir, "05-consistency.txt", _consistency_text)
            _write(out_dir, "06-questions.txt", _question_text)
            _write(out_dir, "07-ranking.txt", "\n".join(ranking["lines"]))
            _write(out_dir, "08-metrics.txt", _metric_text)
            drift_lines = ["# Cross-version facet and entity changes", ""]
            drift_lines += [
                f"- {row['url']} {row['facet']}: {row['kind']} "
                f"before={_dump(row.get('before'))} after={_dump(row.get('after'))}"
                for row in drift
            ] or ["(no changes)"]
            _write(out_dir, "09-drift.txt", "\n".join(drift_lines))
            drift_review_text, drift_review = _drift_review_lines()
            _write(out_dir, "10-drift-review.txt", drift_review_text)
            summaries = [
                f"Generated: {_stamp()}",
                f"Derivation counts: {_dump(counts)}",
                f"Reports: {len(REPORT_NAMES) - 1}",
                f"Drift review: {_dump(drift_review)}",
                (
                    f"Anchor resolution (offline corpus): "
                    f"{'PASS' if not unresolved.get('demo') else 'FAIL: ' + _dump(unresolved['demo'])}"
                ),
                "",
            ]
            for name in REPORT_NAMES[:-1]:
                path = out_dir / name
                summaries.append(
                    f"- {name}: {len(path.read_text(encoding='utf-8').splitlines())} lines"
                )
            summaries += ["", "Invariants:"]
            summaries += [
                f"- {key}: {'PASS' if ok else 'FAIL'}" for key, ok in checks.items()
            ]
            summaries += [
                "",
                "Failures:",
                "  - " + ", ".join(k for k, v in checks.items() if not v),
            ]
            _write(out_dir, "SUMMARY.txt", "\n".join(summaries))
            return {
                "counts": counts,
                "invariants": checks,
                "metrics": values,
                "drift_review": drift_review,
            }
        finally:
            conn.close()


def main() -> int:
    args = _parse_args()
    summary = render(Path(args.out_dir))
    failed = [name for name, ok in summary["invariants"].items() if not ok]
    print(f"doc features: {summary['counts']}")
    print(f"invariants: {'PASS' if not failed else 'FAIL ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
