"""Evaluate document-derived features on the frozen benchmark archive.

The archived page bodies are replayed through the production cleaning and snippet
ingestion paths, then the production document-feature derivation runs on them.
The report compares the same questions with doc_feature_alpha off and on and
records alignment, block-type, coverage, drift and ranking observations. Nothing
here writes gates: a floor needs a deliberate human decision after a full replay.

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


def _seed_entities(conn, docs_version: int, code_version: int) -> int:
    """Give the documentation version the target declarations mined from its code branch."""
    existing = conn.execute(
        "SELECT count(*) FROM api_entities WHERE version_id=?", (docs_version,)
    ).fetchone()[0]
    if existing:
        return int(existing)
    rows = conn.execute(
        "SELECT entity_uid, library_id, qualified_name, kind, file, line_start,"
        " line_end, params, param_types, return_type, bases, parent, signature, has_doc"
        " FROM api_entities WHERE version_id=?",
        (code_version,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "INSERT INTO api_entities(entity_uid, library_id, version_id, qualified_name,"
            " kind, file, line_start, line_end, params, param_types, return_type, bases,"
            " parent, signature, has_doc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"docs:{row[0]}", row[1], docs_version, *row[2:]),
        )
    conn.commit()
    return len(rows)


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
    payload = bench_archive.payload_path(
        entry, _load_json(bench_archive.archive_dir(entry) / bench_archive.META_FILE)
    )
    counts = {"pages": 0, "snippets": 0, "calls": 0}
    for row in bench_archive.iter_pages(payload):
        url = str(row["url"])
        body = str(row["body"] or "")
        kind = classify_content(str(row["content_type"] or ""), url)
        if kind == "html":
            scraped = pipeline.scrape_cached(body, url)
            markdown = scraped.markdown
            snippets = html_code_blocks(
                scraped.cleaned_html,
                sample_prefix=doc_source_key(version_id, url),
                url=url,
                heading=scraped.title,
            )
            origin = "html_code_block"
        else:
            markdown = pipeline.clean_with_cache(kind, body, url)
            snippets = fenced_blocks(
                markdown,
                sample_prefix=doc_source_key(version_id, url),
                url=url,
            )
            origin = "markdown_fence"
        if not markdown.strip():
            continue
        counts["pages"] += 1
        if not snippets:
            continue
        counts["snippets"] += len(snippets)
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
        )
        counts["calls"] += int(result["calls"])
    return counts


def _resolve_anchor(conn, question: dict, version_id: int) -> list[str]:
    problems: list[str] = []
    for url in question.get("targets", []):
        row = conn.execute(
            "SELECT json_extract(metadata, '$.path') FROM documents"
            " WHERE url=? AND version_id=? LIMIT 1",
            (str(url), version_id),
        ).fetchone()
        if row is None:
            problems.append(f"url:{url}")
            continue
        heading = str(question.get("expected_facets", {}).get("heading") or "")
        if heading and heading.lower() not in str(row[0] or "").lower():
            problems.append(f"heading:{heading}")
    for name in question.get("expected_entities", []):
        if (
            conn.execute(
                "SELECT 1 FROM api_entities WHERE qualified_name=? AND version_id=? LIMIT 1",
                (str(name), version_id),
            ).fetchone()
            is None
        ):
            problems.append(f"entity:{name}")
    return problems


def _score_entry(conn, question: dict, version_id: int) -> dict[str, Any]:
    from libref.services.docs.doc_insight import DocInsightService

    insight = DocInsightService(conn)
    doc_ids = []
    for url in question.get("targets", []):
        rows = conn.execute(
            "SELECT id FROM documents WHERE url=? AND version_id=?",
            (str(url), version_id),
        ).fetchall()
        doc_ids.extend(int(row[0]) for row in rows)
    links = {
        str(row["api"])
        for doc_id in doc_ids
        for row in insight.links(version_id, limit=1000)
        if int(row["document_id"]) == doc_id
    }
    want = {str(name) for name in question.get("expected_entities", [])}
    features = {
        (int(row["document_id"]), str(row["facet"])): row["details"].get("value")
        for row in insight.features(version_id, limit=1000)
    }
    expected_type = str(question.get("expected_facets", {}).get("block_type") or "")
    verdicts = {
        int(row["document_id"]): str(row["verdict"])
        for row in insight.consistency(
            version_id, _library_id(conn, version_id), limit=1000
        )
    }
    return {
        "id": str(question["id"]),
        "source_ref": str(question.get("source_ref") or ""),
        "anchor_problems": _resolve_anchor(conn, question, version_id),
        "entities_expected": sorted(want),
        "entities_found": sorted(links),
        "entity_match": links == want,
        "block_type_expected": expected_type,
        "block_type_match": not expected_type
        or any(
            features.get((doc_id, "block_type")) == expected_type for doc_id in doc_ids
        ),
        "verdict_expected": str(question.get("expected_verdict") or ""),
        "verdict_match": not question.get("expected_verdict")
        or any(
            verdicts.get(doc_id) == question["expected_verdict"] for doc_id in doc_ids
        ),
    }


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
    for question in questions:
        best = 2 if question.get("expected_facets", {}).get("heading") else 1
        ranks[str(question["id"])] = {}
        for label, service in services.items():
            found, _status = asyncio.run(
                service.search_with_mode(
                    question["query"], "balanced", **scope, limit=K
                )
            )
            gains = [_grade(item, question) for item in found]
            rows[label].append(
                {"gains": gains, "best": best, "count": len(found), "duplicates": 0}
            )
            ranks[str(question["id"])][label] = next(
                (index + 1 for index, gain in enumerate(gains) if gain > 0), 0
            )
    return {"ranks": ranks, "rows": rows, "version_id": version_id}


def _evaluate_fixture(
    conn, entry: dict, info: dict, questions: list[dict], settings, alpha: float
) -> dict[str, Any]:
    from libref.services.code.mining_refresh import mine_patterns
    from libref.services.docs.feature_refresh import refresh_doc_features

    name = f"{entry['project']}/{entry['kind']}"
    version_id = _version_id(conn, info)
    code = next(
        (
            item
            for item in conn.execute(
                "SELECT v.id FROM versions v JOIN libraries l ON l.id=v.library_id"
                " WHERE l.name=? AND l.ecosystem=? AND v.version!=? AND v.id!=?",
                (info["library"], info["ecosystem"], info["version"], version_id),
            ).fetchall()
        ),
        None,
    )
    entities = _seed_entities(conn, version_id, int(code[0])) if code else 0
    sourced = _ingest_code_blocks(conn, entry, info, version_id, settings)
    mine_patterns(
        conn,
        str(info["library"]),
        str(info["version"]),
        ecosystem=str(info["ecosystem"]),
        profile="usage",
    )
    counts = refresh_doc_features(
        conn,
        str(info["library"]),
        str(info["version"]),
        ecosystem=str(info["ecosystem"]),
        settings=settings,
    )
    entries = [_score_entry(conn, question, version_id) for question in questions]
    ranking = _rank(conn, settings, questions, info, version_id, alpha)
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
        "entities_seeded": entities,
        "snippet_counts": sourced,
        "derivation": counts,
        "questions": entries,
        "ranks": ranking["ranks"],
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

    lines = [
        "# 文档特征冻结归档评测",
        "",
        f"- 生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 评测权重：doc_feature_alpha={alpha:g}（关闭列为 0，开启列仅切换该权重）",
        "- 报告只记录观测值，不写门禁；归档修订变化后需重新标定下限。",
        "- 当前题目只人工确认目标实体，不是整块全量实体清单；集合相等仅作观察，不写门禁。",
        "",
    ]
    for item in results:
        lines += [
            f"## {item['name']}",
            "",
            f"- 归档修订：{item['revision']}",
            f"- 获取地址：{item['source']}",
            f"- 复制实体：{item['entities_seeded']}；代码块：{_dump(item['snippet_counts'])}",
            f"- 派生计数：{_dump(item['derivation'])}",
            "",
            "| 题目 | 来源 | 锚点 | 实体对齐 | 块类型 | 判定 | 排名 off/on |",
            "|---|---|---|---|---|---|---|",
        ]
        by_id = {str(row["id"]): row for rows in questions.values() for row in rows}
        for row in item["questions"]:
            source = by_id.get(str(row["id"]), {}).get("source", "-")
            rank = item["ranks"].get(str(row["id"]), {})
            lines.append(
                f"| {row['id']} | {source} | {'OK' if not row['anchor_problems'] else _dump(row['anchor_problems'])} "
                f"| {'OK' if row['entity_match'] else _dump([row['entities_expected'], row['entities_found']])} "
                f"| {'OK' if row['block_type_match'] else row['block_type_expected']} "
                f"| {'OK' if row['verdict_match'] else row['verdict_expected']} "
                f"| {rank.get('off', 0)}/{rank.get('on', 0)} |"
            )
        metrics = item["metrics"]
        entries = item["questions"]
        lines += [
            "",
            (
                f"- 实体对齐：{sum(1 for row in entries if row['entity_match'])}/{len(entries)}"
                f"（期望与落库边集合相等）"
            ),
            f"- 块类型一致：{sum(1 for row in entries if row['block_type_match'])}/{len(entries)}",
            f"- 判定一致：{sum(1 for row in entries if row['verdict_match'])}/{len(entries)}",
            f"- 覆盖行数：{item['derivation']['coverage']}",
        ]
        if not item["snippet_counts"]["snippets"]:
            lines.append(
                "- 该站原始正文没有语言标记代码块：实体对齐率只作记录，不计入门禁。"
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
            "",
        ]
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
    from libref.db import open_db
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
