#!/usr/bin/env python3
"""Export graph gold-case results for manual review.

Usage:
    uv run python tools/export_graph_audit.py \
        --db /path/to/libref.db \
        --library graphlib --version 1.0 \
        --out /tmp/graph-audit.json \
        [--text /tmp/graph-audit.md] [--strict]

Gold cases may declare their own `scope`; in that case the library and version
arguments only provide defaults and may be omitted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLD = ROOT / "tests" / "fixtures" / "graph_gold.json"


def build_report(
    db_path: Path,
    *,
    library: str | None = None,
    version: str | None = None,
    gold_path: Path = DEFAULT_GOLD,
    ecosystem: str | None = None,
    tenant_id: str = "default",
    profile: str = "usage",
) -> dict[str, Any]:
    """Run the gold cases against one database and return a reviewable report."""
    sys.path.insert(0, str(ROOT / "src"))
    from libref.config import LibrefSettings
    from libref.db import open_db
    from libref.services.factory import build_graph_query_service

    cases = json.loads(gold_path.read_text(encoding="utf-8")).get("cases", [])
    conn = open_db(db_path)
    try:
        service = build_graph_query_service(conn, LibrefSettings())
        report = service.audit(
            cases,
            library=library,
            version=version,
            ecosystem=ecosystem,
            tenant_id=tenant_id,
            profile=profile,
        )
        report["database"] = str(db_path)
        report["gold"] = str(gold_path)
        return report
    finally:
        conn.close()


def _values(items: list[Any]) -> str:
    return ", ".join(str(item) for item in items) if items else "-"


def render_text_report(report: dict[str, Any]) -> str:
    """Render one reviewable Markdown report from the structured report."""
    summary = report["summary"]
    metrics = report.get("metrics", {})
    lines = [
        "# Graph query audit",
        "",
        f"- database: {report.get('database', '-')}",
        f"- gold: {report.get('gold', '-')}",
        f"- library: {report['library'] or '-'}",
        f"- version: {report['version'] or '-'}",
        f"- ecosystem: {report.get('ecosystem') or '-'}",
        f"- profile: {report['profile']}",
        f"- tenant: {report['tenant_id'] or '-'}",
        f"- include ambiguous: {report.get('include_ambiguous', False)}",
        (
            f"- matched: {summary['matched']}/{summary['total']}"
            f" ({summary.get('match_rate', 0.0)})"
        ),
        f"- errors: {summary.get('errors', 0)}",
        f"- seed resolution: {metrics.get('seed_resolution', 0.0)}",
        f"- path hit: {metrics.get('path_hit', 0.0)}",
        (
            f"- node precision/recall:"
            f" {metrics.get('node_precision', 0.0)}/{metrics.get('node_recall', 0.0)}"
        ),
        (
            f"- relation precision/recall:"
            f" {metrics.get('relation_precision', 0.0)}"
            f"/{metrics.get('relation_recall', 0.0)}"
        ),
        (f"- edge evidence traceability: {metrics.get('evidence_traceability', 0.0)}"),
        (
            f"- node evidence traceability:"
            f" {metrics.get('node_evidence_traceability', 0.0)}"
        ),
        f"- unexpected truncations: {metrics.get('unexpected_truncations', 0)}",
        f"- missed truncations: {metrics.get('missed_truncations', 0)}",
        "",
        "## Cases",
        "",
    ]
    for case in report["cases"]:
        lines.extend(
            [
                f"### {case['name']}",
                "",
                f"- source ref: {case.get('source_ref') or '-'}",
                (
                    "- scope: "
                    f"library={case.get('scope', {}).get('library') or '-'},"
                    f" version={case.get('scope', {}).get('version') or '-'},"
                    f" profile={case.get('scope', {}).get('profile') or '-'},"
                    f" tenant={case.get('scope', {}).get('tenant_id') or '-'}"
                ),
                f"- passed: {case['passed']}",
                f"- mode: {case.get('mode', '-')}",
                f"- target: {case.get('target') or '-'}",
                f"- direction: {case.get('direction', '-')}",
                f"- depth: {case.get('depth') or '-'}",
                (f"- relation types: {_values(case.get('relation_types', []))}"),
                f"- include ambiguous: {case.get('include_ambiguous', False)}",
                f"- error: {case.get('error') or '-'}",
                (
                    f"- seed: {case.get('actual_seed', '-')}"
                    f" (expected {case.get('expected_seed', '-')})"
                ),
                (
                    f"- found: {case.get('found', False)}"
                    f" (expected {case.get('expected_found', '-')})"
                ),
                (
                    f"- nodes: {_values(case.get('actual_nodes', []))}"
                    f" (expected {_values(case.get('expected_nodes', []))})"
                ),
                f"- forbidden nodes: {_values(case.get('forbidden_nodes', []))}",
                (
                    f"- relations: {_values(case.get('actual_relations', []))}"
                    f" (expected {_values(case.get('expected_relations', []))})"
                ),
                (
                    f"- evidence: {_values(case.get('actual_evidence_refs', []))}"
                    f" (expected {_values(case.get('expected_evidence_refs', []))})"
                ),
                (
                    f"- edge evidence traceability:"
                    f" {case.get('evidence_traceability', 0.0)}"
                ),
                (
                    f"- node evidence traceability:"
                    f" {case.get('node_evidence_traceability', 0.0)}"
                ),
                f"- synthetic nodes: {_values(case.get('synthetic_nodes', []))}",
                (
                    f"- truncated: {case.get('truncated', False)}"
                    f" (expected {case.get('expected_truncated', False)})"
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--library")
    parser.add_argument("--version")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--ecosystem")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile", default="usage")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--text", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    report = build_report(
        args.db,
        library=args.library,
        version=args.version,
        gold_path=args.gold,
        ecosystem=args.ecosystem,
        tenant_id=args.tenant_id,
        profile=args.profile,
    )
    text_path = args.text or args.out.with_suffix(".md")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(render_text_report(report), encoding="utf-8")
    summary = report["summary"]
    print(
        f"graph audit: {summary['matched']}/{summary['total']} matched,"
        f" json={args.out} text={text_path}"
    )
    if args.strict and (
        summary["total"] == 0
        or summary["matched"] != summary["total"]
        or summary.get("errors", 0)
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
