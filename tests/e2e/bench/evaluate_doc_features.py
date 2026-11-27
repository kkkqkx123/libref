"""Evaluate document-derived features on the frozen benchmark archive.

The archived page bodies are replayed through the production cleaning and snippet
ingestion paths, then the production document-feature derivation runs on them.
The report compares the same questions with doc_feature_alpha off and on and
records alignment, feature-node, block-type, coverage, drift and ranking
observations. Expected feature nodes are checked as a subset of the nodes linked
to the question's target documents. Unlabeled expectations are reported as
unmeasurable and excluded from accuracy denominators. Nothing here writes gates:
a floor needs a deliberate human decision after a full replay.

Usage:
    uv run python -m tests.e2e.bench.evaluate_doc_features [--data DIR] [--out DIR]

A missing archive or dataset is reported as an unavailable environment and skips
the run without producing a report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parents[2]) not in sys.path:
    # Direct execution keeps the same import root as the module invocation.
    sys.path.insert(0, str(HERE.parents[2]))
DATA = HERE / "data"
OUTPUT = HERE / "outputs" / "retrieval-bench" / "doc-features"
QUESTIONS = HERE.parent / "fixtures" / "doc_features" / "questions.json"
DRIFT_SAMPLES = HERE.parent / "fixtures" / "doc_features" / "drift_samples.json"
K = 5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="evaluate document feature derivation")
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--out", default=str(OUTPUT))
    parser.add_argument("--alpha", type=float, default=0.4)
    return parser.parse_args()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _version_id(conn, info: dict[str, Any]) -> int:
    row = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id=v.library_id"
        " WHERE l.name=? AND l.ecosystem=? AND v.version=?",
        (info["library"], info["ecosystem"], info["version"]),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"corpus not in dataset: {info['library']}:{info['version']}"
        )
    return int(row[0])


def _bind_entity_source(conn, entry: dict, info: dict) -> str:
    """Bind one docs version to the code version that owns its API declarations."""
    from libref.services.registry.libraries import LibraryService
    from tests.e2e.bench import archive as bench_archive

    source_ref = str(entry.get("entity_source") or "")
    if not source_ref:
        raise RuntimeError(f"{entry['project']}/docs has no entity_source")
    source = next(
        (
            item
            for item in bench_archive.select([])
            if bench_archive.fixture_id(item) == source_ref
        ),
        None,
    )
    if source is None or source.get("kind") != "code":
        raise RuntimeError(f"{source_ref} is not a source fixture")
    LibraryService(conn).bind_entity_source(
        str(info["library"]),
        str(info["version"]),
        str(source["version"]),
        ecosystem=str(info["ecosystem"]),
        source_ecosystem=str(source["ecosystem"]),
    )
    return source_ref


def _ingest_code_blocks(
    conn, entry: dict, info: dict, version_id: int, settings
) -> dict:
    """Register the archived raw fences and HTML blocks as doc_snippet usage evidence."""
    from libref.astminer.code_blocks import fenced_blocks
    from libref.crawler.code_blocks import html_code_blocks
    from libref.services.code.usage_ingest import doc_source_key, ingest_snippets
    from libref.services.docs.page_pipeline import PagePipeline
    from libref.text.normalize import classify_content
    from tests.e2e.bench import archive as bench_archive

    pipeline = PagePipeline(settings)
    meta = _load_json(bench_archive.archive_dir(entry) / bench_archive.META_FILE)
    payload = bench_archive.payload_path(entry, meta)
    candidates = list(meta["recipe"].get("snippet_languages") or ())
    rules = dict(meta["recipe"].get("snippet_language_rules") or {})
    counts: dict[str, int] = {"pages": 0}
    for row in bench_archive.iter_pages(payload):
        url = str(row["url"])
        body = str(row["body"] or "")
        kind = classify_content(str(row["content_type"] or ""), url)
        if kind == "html":
            scraped = pipeline.scrape_cached(body, url)
            markdown = scraped.markdown
            snippets = html_code_blocks(
                body,
                sample_prefix=doc_source_key(version_id, url),
                url=url,
                heading=scraped.title,
                manual_rules=rules,
            )
            origin = "html_code_block"
        else:
            markdown = pipeline.clean_with_cache(kind, body, url)
            snippets = fenced_blocks(
                body,
                sample_prefix=doc_source_key(version_id, url),
                url=url,
            )
            origin = "markdown_fence"
        if not markdown.strip():
            continue
        counts["pages"] += 1
        if not snippets:
            continue
        result = ingest_snippets(
            conn,
            str(info["library"]),
            str(info["version"]),
            snippets,
            source_key=doc_source_key(version_id, url),
            source_role="doc_snippet",
            origin_kind=origin,
            ecosystem=str(info["ecosystem"]),
            source_url=url,
            selection={"url": url},
            settings=settings,
            snippet_candidates=candidates,
            snippet_language_rules=rules,
        )
        for key, value in result.items():
            if key == "source_id":
                continue
            counts[key] = counts.get(key, 0) + int(value)
    return counts


def _snippet_funnel(
    conn, version_id: int, snippets: int, coverage: int
) -> dict[str, int]:
    """Observed path from snippets through resolved calls to pattern coverage."""
    calls = conn.execute(
        "SELECT count(*), sum(CASE WHEN target_status='resolved_target' THEN 1 ELSE 0 END),"
        " count(DISTINCT sample_id) FROM call_instances"
        " WHERE version_id=? AND source_role='doc_snippet'",
        (version_id,),
    ).fetchone()
    effective = conn.execute(
        "SELECT count(*) FROM ("
        " SELECT sample_id FROM call_instances"
        " WHERE version_id=? AND source_role='doc_snippet'"
        " AND target_status='resolved_target'"
        " GROUP BY sample_id HAVING count(*) >= 2)",
        (version_id,),
    ).fetchone()
    patterns = conn.execute(
        "SELECT count(*) FROM seq_patterns WHERE version_id=?", (version_id,)
    ).fetchone()
    return {
        "snippets": int(snippets),
        "calls": int(calls[0] or 0),
        "resolved_target": int(calls[1] or 0),
        "samples": int(calls[2] or 0),
        "effective_samples": int(effective[0] or 0),
        "patterns": int(patterns[0] or 0) if patterns else 0,
        "coverage": int(coverage),
    }


def _snippet_evidence(conn, version_id: int) -> dict[str, dict[str, int]]:
    """Snippet counts by language evidence source and parse status."""
    rows = conn.execute(
        "SELECT language_source, parse_status, count(*) FROM doc_snippet_records"
        " WHERE version_id=? GROUP BY language_source, parse_status",
        (version_id,),
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for source, status, count in rows:
        out.setdefault(str(source or "none"), {})[str(status)] = int(count)
    return out


def _resolve_anchor(conn, question: dict, version_id: int) -> list[str]:
    from libref.services.registry.libraries import LibraryService

    problems: list[str] = []
    entity_version_id = LibraryService(conn).entity_source_version_id(version_id)
    for url in question.get("targets", []):
        paths = [
            str(row[0] or "")
            for row in conn.execute(
                "SELECT DISTINCT json_extract(metadata, '$.path') FROM documents"
                " WHERE url=? AND version_id=?",
                (str(url), version_id),
            ).fetchall()
        ]
        if not paths:
            problems.append(f"url:{url}")
            continue
        heading = str(question.get("expected_facets", {}).get("heading") or "")
        if heading and not any(heading.lower() in path.lower() for path in paths):
            problems.append(f"heading:{heading}")
    for name in question.get("expected_entities", []):
        if (
            conn.execute(
                "SELECT 1 FROM api_entities WHERE qualified_name=? AND version_id=? LIMIT 1",
                (str(name), entity_version_id),
            ).fetchone()
            is None
        ):
            problems.append(f"entity:{name}")
    return problems


def _score_entry(conn, question: dict, version_id: int) -> dict[str, Any]:
    from libref.services.docs.doc_insight import DocInsightService

    insight = DocInsightService(conn)
    doc_ids: list[int] = []
    for url in question.get("targets", []):
        rows = conn.execute(
            "SELECT id FROM documents WHERE url=? AND version_id=?",
            (str(url), version_id),
        ).fetchall()
        doc_ids.extend(int(row[0]) for row in rows)
    library_id = _library_id(conn, version_id)
    links: set[str] = set()
    features: dict[tuple[int, str], object] = {}
    verdicts: dict[int, str] = {}
    for doc_id in doc_ids:
        for row in insight.links(version_id, document_id=doc_id, limit=1000):
            links.add(str(row["api"]))
        for row in insight.features(version_id, document_id=doc_id, limit=1000):
            features[(doc_id, str(row["facet"]))] = row["details"].get("value")
        for row in insight.consistency(
            version_id, library_id, document_id=doc_id, limit=1000
        ):
            verdicts[doc_id] = str(row["verdict"])
    strong: set[str] = set()
    if doc_ids:
        marks = ",".join("?" for _ in doc_ids)
        strong = {
            str(row[0])
            for row in conn.execute(
                "SELECT ae.qualified_name FROM doc_api_links l"
                " JOIN api_entities ae ON ae.id=l.api_entity_id"
                f" WHERE l.version_id=? AND l.document_id IN ({marks})"
                " AND l.match_kind IN ('symbol', 'signature')",
                [version_id, *doc_ids],
            ).fetchall()
        }
    want = {str(name) for name in question.get("expected_entities", [])}
    expected_facets = {
        str(key): value
        for key, value in question.get("expected_facets", {}).items()
        if key != "heading"
    }
    facet_checks = {
        key: any(features.get((doc_id, key)) == value for doc_id in doc_ids)
        for key, value in expected_facets.items()
    }
    expected_type = str(expected_facets.get("block_type") or "")
    node_paths: set[str] = set()
    for doc_id in doc_ids:
        for row in conn.execute(
            "SELECT node_path FROM doc_feature_node_links"
            " WHERE version_id=? AND document_id=?",
            (version_id, doc_id),
        ).fetchall():
            node_paths.add(str(row[0]))
    want_nodes = {str(path) for path in question.get("expected_feature_nodes", [])}
    want_verdict = str(question.get("expected_verdict") or "")
    return {
        "id": str(question["id"]),
        "source_ref": str(question.get("source_ref") or ""),
        "anchor_problems": _resolve_anchor(conn, question, version_id),
        "entities_expected": sorted(want),
        "entities_found": sorted(links),
        "entities_extra": sorted(links - want),
        "entities_found_strong": sorted(strong),
        "entity_match": want <= links if want else None,
        "entity_match_strong": want <= strong if want else None,
        "facets_expected": expected_facets,
        "facet_checks": facet_checks,
        "facet_match": all(facet_checks.values()),
        "block_type_expected": expected_type,
        "block_type_match": bool(expected_type)
        and facet_checks.get("block_type", False),
        "verdict_expected": want_verdict,
        "verdict_match": (
            any(verdicts.get(doc_id) == want_verdict for doc_id in doc_ids)
            if want_verdict
            else None
        ),
        "nodes_expected": sorted(want_nodes),
        "nodes_found": sorted(node_paths),
        "node_match": want_nodes <= node_paths if want_nodes else None,
    }


def _match_stats(entries: list[dict[str, Any]], key: str) -> tuple[int, int]:
    measured = [entry.get(key) for entry in entries if entry.get(key) is not None]
    return sum(1 for value in measured if value), len(measured)


def _match_line(name: str, key: str, entries: list[dict[str, Any]]) -> str:
    hits, total = _match_stats(entries, key)
    suffix = "（有标签题，不可测量）" if not total else "（有标签题）"
    return f"- {name}：{hits}/{total}{suffix}"


def _library_id(conn, version_id: int) -> int:
    return int(
        conn.execute(
            "SELECT library_id FROM versions WHERE id=?", (version_id,)
        ).fetchone()[0]
    )


def _grade(item: dict, question: dict) -> int:
    targets = {str(url) for url in question.get("targets", [])}
    if str(item.get("url") or "") not in targets:
        return 0
    heading = str(question.get("expected_facets", {}).get("heading") or "")
    if heading and heading.lower() not in str(item.get("path") or "").lower():
        return int((question.get("grades") or {}).get("general", 1))
    return int((question.get("grades") or {}).get("strong", 2))


def _target_doc_ids(conn, question: dict, version_id: int) -> set[int]:
    heading = str(question.get("expected_facets", {}).get("heading") or "")
    out: set[int] = set()
    for url in question.get("targets", []):
        sql = "SELECT id FROM documents WHERE url=? AND version_id=?"
        params: list[Any] = [str(url), version_id]
        if heading:
            sql += " AND lower(json_extract(metadata, '$.path')) LIKE ?"
            params.append(f"%{heading.lower()}%")
        out.update(int(row[0]) for row in conn.execute(sql, params).fetchall())
    return out


def _eligibility(
    conn, question: dict, version_id: int, target_ids: set[int]
) -> dict[str, bool]:
    facets = question.get("expected_facets", {})
    strong = False
    if target_ids:
        marks = ",".join("?" for _ in target_ids)
        strong = bool(
            conn.execute(
                "SELECT 1 FROM doc_api_links"
                f" WHERE version_id=? AND document_id IN ({marks})"
                " AND match_kind IN ('symbol', 'signature') LIMIT 1",
                [version_id, *target_ids],
            ).fetchone()
        )
    return {
        "examples_only": str(facets.get("block_type") or "") == "example",
        "exclude_deprecated": not bool(facets.get("deprecated")),
        "aligned_only": strong,
    }


def _filter_stats(
    conn, found: list[dict], question: dict, eligible: dict[str, bool]
) -> dict[str, Any]:
    from libref.store.retriever import filter_by_doc_facets

    targets = {str(url) for url in question.get("targets", [])}
    heading = str(question.get("expected_facets", {}).get("heading") or "")
    ids = [int(item["id"]) for item in found]
    url_by_id = {int(item["id"]): str(item.get("url") or "") for item in found}
    path_by_id = {int(item["id"]): str(item.get("path") or "") for item in found}
    options = {
        "examples_only": {"block_type": "example"},
        "exclude_deprecated": {"exclude_deprecated": True},
        "aligned_only": {"aligned_only": True},
    }
    out: dict[str, Any] = {}
    for name, kwargs in options.items():
        kept = filter_by_doc_facets(conn, ids, **kwargs)
        hit = any(
            url_by_id[doc_id] in targets
            and (not heading or heading.lower() in path_by_id[doc_id].lower())
            for doc_id in kept
        )
        out[name] = {
            "results": len(ids),
            "kept": len(kept),
            "hits": int(hit),
            "eligible": bool(eligible[name]),
            "eligible_hits": int(hit and eligible[name]),
        }
    return out


def _attribution(
    target_ids: set[int], fused_order: list[int], off: int, on: int
) -> str:
    if not target_ids.intersection(fused_order):
        return "outside_pool"
    if off and not on:
        return "lost"
    if not off and on:
        return "entered"
    if on > off:
        return "demoted"
    if on < off:
        return "promoted"
    return "unchanged"


def _rank(
    conn, settings, questions: list[dict], info: dict, version_id: int, alpha: float
) -> dict[str, Any]:
    from libref.services.docs.search import SearchService
    from tests.e2e.bench.fakes import BagOfTermsEmbedder, SqliteVecStore

    embedder = BagOfTermsEmbedder()
    vectors = SqliteVecStore(conn, embedder)
    asyncio.run(vectors.prepare())
    scope = {
        "library": str(info["library"]),
        "version": str(info["version"]),
        "ecosystem": str(info["ecosystem"]),
    }
    on_settings = settings.model_copy(update={"doc_feature_alpha": alpha})
    services = {
        "off": SearchService(conn, settings, vec_store=vectors, embed=embedder),
        "on": SearchService(conn, on_settings, vec_store=vectors, embed=embedder),
    }
    rows: dict[str, list[dict]] = {"off": [], "on": []}
    ranks: dict[str, dict[str, int]] = {}
    filter_rows: list[dict] = []
    attribution_rows: list[dict[str, Any]] = []
    found_by_query: dict[str, list[dict]] = {}
    labels = ("outside_pool", "lost", "entered", "demoted", "promoted", "unchanged")
    attribution = {label: 0 for label in labels}
    for question in questions:
        best = 2 if question.get("expected_facets", {}).get("heading") else 1
        ranks[str(question["id"])] = {}
        target_ids = _target_doc_ids(conn, question, version_id)
        statuses: dict[str, dict[str, Any]] = {}
        for label, service in services.items():
            found, statuses[label] = asyncio.run(
                service.search_with_mode(
                    question["query"], "balanced", **scope, limit=K
                )
            )
            found_by_query[label + str(question["id"])] = found
            gains = [_grade(item, question) for item in found]
            rows[label].append(
                {"gains": gains, "best": best, "count": len(found), "duplicates": 0}
            )
            ranks[str(question["id"])][label] = next(
                (index + 1 for index, gain in enumerate(gains) if gain > 0), 0
            )
        off, on = ranks[str(question["id"])]["off"], ranks[str(question["id"])]["on"]
        category = _attribution(
            target_ids, list(statuses["on"].get("fused_order") or []), off, on
        )
        attribution[category] += 1
        attribution_rows.append(
            {"id": str(question["id"]), "category": category, "off": off, "on": on}
        )
        eligible = _eligibility(conn, question, version_id, target_ids)
        filter_rows.append(
            _filter_stats(
                conn, found_by_query["off" + str(question["id"])], question, eligible
            )
        )
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
            "eligible_questions": sum(
                int(row[name]["eligible"]) for row in filter_rows
            ),
            "eligible_hits": sum(row[name]["eligible_hits"] for row in filter_rows),
            "eligible_hit_rate": (
                sum(row[name]["eligible_hits"] for row in filter_rows)
                / max(1, sum(int(row[name]["eligible"]) for row in filter_rows))
            ),
        }
        for name in ("examples_only", "exclude_deprecated", "aligned_only")
    }
    return {
        "ranks": ranks,
        "rows": rows,
        "version_id": version_id,
        "filters": filters,
        "attribution": attribution,
        "attribution_rows": attribution_rows,
    }


def _evaluate_fixture(
    conn, entry: dict, info: dict, questions: list[dict], settings, alpha: float
) -> dict[str, Any]:
    from libref.services.code.mining_refresh import mine_patterns
    from libref.services.docs.feature_refresh import refresh_doc_features

    name = f"{entry['project']}/{entry['kind']}"
    version_id = _version_id(conn, info)
    stages: dict[str, int] = {}
    started = time.monotonic()
    entity_source = _bind_entity_source(conn, entry, info)
    stages["bind_ms"] = int((time.monotonic() - started) * 1000)
    started = time.monotonic()
    sourced = _ingest_code_blocks(conn, entry, info, version_id, settings)
    stages["ingest_ms"] = int((time.monotonic() - started) * 1000)
    started = time.monotonic()
    mine_patterns(
        conn,
        str(info["library"]),
        str(info["version"]),
        ecosystem=str(info["ecosystem"]),
        profile="usage",
    )
    stages["mine_ms"] = int((time.monotonic() - started) * 1000)
    started = time.monotonic()
    counts = refresh_doc_features(
        conn,
        str(info["library"]),
        str(info["version"]),
        ecosystem=str(info["ecosystem"]),
        settings=settings,
    )
    stages["refresh_ms"] = int((time.monotonic() - started) * 1000)
    started = time.monotonic()
    entries = [_score_entry(conn, question, version_id) for question in questions]
    ranking = _rank(conn, settings, questions, info, version_id, alpha)
    stages["evaluate_ms"] = int((time.monotonic() - started) * 1000)
    from tests.e2e.bench.metrics import (
        metric_series,
        paired_delta,
        summarize,
    )

    before = summarize(ranking["rows"]["off"], K)
    after = summarize(ranking["rows"]["on"], K)
    return {
        "name": name,
        "revision": str(info.get("revision") or ""),
        "source": str(info.get("source") or ""),
        "entity_source": entity_source,
        "snippet_counts": sourced,
        "snippet_funnel": _snippet_funnel(
            conn, version_id, int(sourced.get("snippets", 0)), int(counts["coverage"])
        ),
        "snippet_evidence": _snippet_evidence(conn, version_id),
        "derivation": counts,
        "questions": entries,
        "ranks": ranking["ranks"],
        "filters": ranking["filters"],
        "attribution": ranking["attribution"],
        "attribution_rows": ranking["attribution_rows"],
        "stages": stages,
        "metrics": {
            "off": before,
            "on": after,
            "ndcg_delta": paired_delta(
                metric_series(ranking["rows"]["on"], K)["ndcg"],
                metric_series(ranking["rows"]["off"], K)["ndcg"],
            ),
            "mrr_delta": paired_delta(
                metric_series(ranking["rows"]["on"], K)["mrr"],
                metric_series(ranking["rows"]["off"], K)["mrr"],
            ),
        },
    }


def _report(results: list[dict], questions: dict, alpha: float, out_dir: Path) -> Path:
    from datetime import UTC, datetime

    samples = json.loads(DRIFT_SAMPLES.read_text(encoding="utf-8")).get("samples", [])
    auto_drift = [row for row in samples if row.get("auto_verdict") == "drift"]
    false_positive = [row for row in auto_drift if row.get("human_verdict") != "drift"]
    false_positive_rate = len(false_positive) / len(auto_drift) if auto_drift else 0.0
    human_drift = [row for row in samples if row.get("human_verdict") == "drift"]
    false_negative = [row for row in human_drift if row.get("auto_verdict") != "drift"]
    false_negative_rate = len(false_negative) / len(human_drift) if human_drift else 0.0
    lines = [
        "# 文档特征冻结归档评测",
        "",
        f"- 生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 评测权重：doc_feature_alpha={alpha:g}（关闭列为 0，开启列仅切换该权重）",
        "- 报告只记录观测值，不写门禁；归档修订变化后需重新标定下限。",
        "- 题目标签按目标文档的派生证据核对；一致性只表示标签与派生表相符，语义正确性需独立复核。",
        (
            f"- 漂移抽检：样本 {len(samples)}，自动 drift {len(auto_drift)}，"
            f"误报 {len(false_positive)}（{false_positive_rate:.1%}），"
            f"漏报 {len(false_negative)}（{false_negative_rate:.1%}）；"
            "样本不足，只记录误报与漏报，不写门禁。"
        ),
        "",
    ]
    for row in false_positive:
        lines.append(
            f"- 漂移误报：{row.get('corpus', '-')} | "
            f"{row.get('url', '-')} | {row.get('heading', '-')} | "
            f"{row.get('reason', '-')}"
        )
    for row in false_negative:
        lines.append(
            f"- 漂移漏报：{row.get('corpus', '-')} | "
            f"{row.get('url', '-')} | {row.get('heading', '-')} | "
            f"{row.get('reason', '-')}"
        )
    if false_positive or false_negative:
        lines.append("")
    all_questions = [row for item in results for row in item["questions"]]
    entity_labeled = sum(1 for row in all_questions if row["entities_expected"])
    node_labeled = sum(1 for row in all_questions if row["nodes_expected"])
    verdict_labeled = sum(1 for row in all_questions if row["verdict_expected"])
    lines.extend(
        [
            f"- 题目总数：{len(all_questions)}",
            f"- 实体标签覆盖：{entity_labeled}/{len(all_questions)}",
            f"- 判定标签覆盖：{verdict_labeled}/{len(all_questions)}",
            f"- 树节点标签覆盖：{node_labeled}/{len(all_questions)}",
            "",
        ]
    )
    for item in results:
        lines += [
            f"## {item['name']}",
            "",
            f"- 归档修订：{item['revision']}",
            f"- 获取地址：{item['source']}",
            f"- 实体来源：{item['entity_source']}；代码块：{_dump(item['snippet_counts'])}",
            f"- 解析漏斗：{_dump(item.get('snippet_funnel', {}))}",
            f"- 语言证据：{_dump(item.get('snippet_evidence', {}))}",
            f"- 派生计数：{_dump(item['derivation'])}",
            f"- 阶段耗时：{_dump(item.get('stages', {}))}",
            "",
            "| 题目 | 来源 | 锚点 | 实体对齐 | 强边对齐 | 树节点 | 特征一致 | 块类型 | 判定 | 排名 off/on |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        by_id = {str(row["id"]): row for rows in questions.values() for row in rows}
        for row in item["questions"]:
            source = by_id.get(str(row["id"]), {}).get("source", "-")
            rank = item["ranks"].get(str(row["id"]), {})
            entity_cell = (
                "unmeasurable"
                if row["entity_match"] is None
                else (
                    "OK"
                    if row["entity_match"]
                    else _dump([row["entities_expected"], row["entities_found"]])
                )
            )
            strong_match = row.get("entity_match_strong")
            strong_cell = (
                "unmeasurable"
                if strong_match is None
                else ("OK" if strong_match else "weak")
            )
            node_cell = (
                "unmeasurable"
                if row["node_match"] is None
                else (
                    "OK"
                    if row["node_match"]
                    else _dump([row["nodes_expected"], row["nodes_found"]])
                )
            )
            verdict_cell = (
                "unmeasurable"
                if row["verdict_match"] is None
                else ("OK" if row["verdict_match"] else row["verdict_expected"])
            )
            lines.append(
                f"| {row['id']} | {source} | {'OK' if not row['anchor_problems'] else _dump(row['anchor_problems'])} "
                f"| {entity_cell} "
                f"| {strong_cell} "
                f"| {node_cell} "
                f"| {'OK' if row['facet_match'] else _dump([row['facets_expected'], row['facet_checks']])} "
                f"| {'OK' if row['block_type_match'] else row['block_type_expected']} "
                f"| {verdict_cell} "
                f"| {rank.get('off', 0)}/{rank.get('on', 0)} |"
            )
        metrics = item["metrics"]
        entries = item["questions"]
        lines += [
            "",
            _match_line("实体对齐", "entity_match", entries),
            _match_line("强边对齐", "entity_match_strong", entries),
            _match_line("树节点一致", "node_match", entries),
            f"- 块特征一致：{sum(1 for row in entries if row['facet_match'])}/{len(entries)}",
            f"- 块类型一致：{sum(1 for row in entries if row['block_type_match'])}/{len(entries)}",
            _match_line("判定一致", "verdict_match", entries),
            f"- 覆盖行数：{item['derivation']['coverage']}",
        ]
        for name, stats in item["filters"].items():
            lines.append(
                f"- 过滤 {name}：保留 {stats['kept']}/{stats['results']}，"
                f"命中 {stats['hits']}，全题命中率 {stats['hit_rate']:.3f}，"
                f"合格题 {stats['eligible_questions']}，合格题命中率 "
                f"{stats['eligible_hit_rate']:.3f}"
            )
        statuses = {
            key: value
            for key, value in item["snippet_counts"].items()
            if key
            in (
                "ok",
                "partial",
                "unsupported",
                "unlabeled",
                "ambiguous",
                "failed",
                "non_target",
            )
        }
        if statuses:
            lines.append(f"- 片段解析状态：{_dump(statuses)}")
        if not item["snippet_counts"]["snippets"]:
            lines.append(
                "- 该站原始正文没有可抽取代码块：实体对齐率只作记录，不计入门禁。"
            )
        lines += [
            "",
            (
                f"- 关闭：nDCG@{K}={metrics['off']['ndcg_at_k']:.3f}，"
                f"MRR@{K}={metrics['off']['mrr_at_k']:.3f}"
            ),
            (
                f"- 开启：nDCG@{K}={metrics['on']['ndcg_at_k']:.3f}，"
                f"MRR@{K}={metrics['on']['mrr_at_k']:.3f}"
            ),
            f"- nDCG 配对差分：{_dump(metrics['ndcg_delta'])}",
            f"- MRR 配对差分：{_dump(metrics['mrr_delta'])}",
            f"- 排名归因：{_dump(item.get('attribution', {}))}",
        ]
        for row in item.get("attribution_rows", []):
            if row.get("category") != "unchanged":
                lines.append(
                    f"- 排名明细：{row.get('id', '-')} {row.get('category', '-')} "
                    f"{row.get('off', 0)}/{row.get('on', 0)}"
                )
        lines.append("")
    lines += ["## 汇总", ""]
    for item in results:
        metrics = item["metrics"]
        delta = metrics["ndcg_delta"]
        lines.append(
            f"- {item['name']}：nDCG 差分 {delta['delta']:+.3f} "
            f"[{delta['lo']:+.3f}, {delta['hi']:+.3f}]，"
            f"显著={'是' if delta['significant'] else '否'}"
        )
    if not results:
        lines.append("- 没有可评测的归档夹具")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def run(data: Path, out_dir: Path, alpha: float) -> list[dict[str, Any]]:
    """Replay every archived docs fixture that has a frozen judgment set."""
    from libref.db import apply_migrations, open_db
    from tests.e2e.bench import archive as bench_archive
    from tests.e2e.bench.fakes import bench_settings

    manifest_path = data / "manifest.json"
    if not manifest_path.is_file():
        print(f"SKIP benchmark dataset unavailable: {manifest_path}", file=sys.stderr)
        return []
    manifest = _load_json(manifest_path)
    questions = _load_json(QUESTIONS)
    db_path = data / "bench.db"
    settings = bench_settings(db_path, doc_feature_alpha=0.0)
    conn = open_db(db_path)
    applied = apply_migrations(conn)
    if applied:
        print(f"applied migrations: {', '.join(str(item) for item in applied)}")
    results: list[dict[str, Any]] = []
    try:
        for entry in bench_archive.select([]):
            if entry["kind"] != "docs":
                continue
            name = bench_archive.fixture_id(entry)
            if name not in questions or not manifest["fixtures"].get(name):
                continue
            try:
                bench_archive.verify(entry)
            except bench_archive.ArchiveError as exc:
                print(f"SKIP {name}: {exc}", file=sys.stderr)
                continue
            started = time.monotonic()
            result = _evaluate_fixture(
                conn,
                entry,
                manifest["fixtures"][name],
                questions[name],
                settings,
                alpha,
            )
            result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            results.append(result)
            print(f"{name}: {result['derivation']} in {result['elapsed_ms']}ms")
    finally:
        conn.close()
    _report(results, questions, alpha, out_dir)
    return results


def main() -> int:
    args = _parse_args()
    results = run(Path(args.data), Path(args.out), float(args.alpha))
    if not results:
        print("SKIP no archived fixture with doc-feature judgments", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
