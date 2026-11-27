#!/usr/bin/env python3
"""Export graph gold-case results for manual review.

Usage:
    uv run python tools/export_graph_audit.py \
        --db /path/to/libref.db \
        --library graphlib --version 1.0 \
        --out /tmp/graph-audit.json
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
    library: str,
    version: str,
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
        return service.audit(
            cases,
            library=library,
            version=version,
            ecosystem=ecosystem,
            tenant_id=tenant_id,
            profile=profile,
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--ecosystem")
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--profile", default="usage")
    parser.add_argument("--out", type=Path, required=True)
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
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary = report["summary"]
    print(
        f"graph audit: {summary['matched']}/{summary['total']} matched,"
        f" report={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
