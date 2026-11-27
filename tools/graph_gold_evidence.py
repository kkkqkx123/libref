#!/usr/bin/env python3
"""List real graph evidence candidates for manual gold authoring.

The script is read-only against the database and writes only to the ignored
output directory. It never writes or updates a gold fixture.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _scope(
    conn: sqlite3.Connection, library: str, version: str, tenant_id: str
) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id=v.library_id"
        " WHERE l.name=? AND v.version=?",
        (library, version),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            f"expected one version for {library} {version}, got {len(rows)}"
        )
    version_id = int(rows[0][0])
    profiles = conn.execute(
        "SELECT id FROM mining_profiles WHERE version_id=? AND name=? AND tenant_id=?",
        (version_id, "usage", tenant_id),
    ).fetchall()
    if len(profiles) != 1:
        raise ValueError(f"expected one usage profile for version {version_id}")
    return version_id, int(profiles[0][0])


def _rows(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]
) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, params)
    names = [str(column[0]) for column in cursor.description or []]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def collect(
    conn: sqlite3.Connection,
    version_id: int,
    profile_id: int,
    *,
    doc_only: bool = False,
) -> dict[str, Any]:
    """Collect candidate rows that can support graph gold cases."""
    inbound: list[dict[str, Any]] = []
    outbound: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    inherit: list[dict[str, Any]] = []
    excluded_calls = 0
    if not doc_only:
        inbound = _rows(
            conn,
            """SELECT ci.callee_entity_id AS entity_id, ae.qualified_name,
                      count(*) AS calls, min(ci.id) AS first_call_id,
                      sum(CASE WHEN rs.status='conflict' THEN 1 ELSE 0 END) AS conflicts
               FROM call_instances ci
               JOIN api_entities ae ON ae.id=ci.callee_entity_id
               LEFT JOIN review_status rs ON rs.entity_type='call_instance'
                 AND rs.entity_id=ci.id AND rs.tenant_id=ci.tenant_id
               WHERE ci.version_id=? AND ci.in_target_api=1 AND ae.version_id=?
               GROUP BY ci.callee_entity_id, ae.qualified_name
               HAVING conflicts=0
               ORDER BY calls DESC, entity_id LIMIT 12""",
            (version_id, version_id),
        )
        outbound = _rows(
            conn,
            """SELECT ci.caller_entity_id AS entity_id, ae.qualified_name,
                      count(*) AS calls, min(ci.id) AS first_call_id,
                      sum(CASE WHEN rs.status='conflict' THEN 1 ELSE 0 END) AS conflicts
               FROM call_instances ci
               JOIN api_entities ae ON ae.id=ci.caller_entity_id
               LEFT JOIN review_status rs ON rs.entity_type='call_instance'
                 AND rs.entity_id=ci.id AND rs.tenant_id=ci.tenant_id
               WHERE ci.version_id=? AND ci.in_target_api=1 AND ae.version_id=?
               GROUP BY ci.caller_entity_id, ae.qualified_name
               HAVING conflicts=0
               ORDER BY calls DESC, entity_id LIMIT 12""",
            (version_id, version_id),
        )
        imports = _rows(
            conn,
            """SELECT ie.target, count(*) AS importers, min(ie.id) AS first_import_id
               FROM import_edges ie
               WHERE ie.version_id=?
                 AND EXISTS (SELECT 1 FROM api_entities ae
                             WHERE ae.version_id=? AND ae.qualified_name=ie.target)
               GROUP BY ie.target ORDER BY importers DESC, ie.target LIMIT 12""",
            (version_id, version_id),
        )
        excluded_calls = int(
            conn.execute(
                "SELECT count(*) FROM call_instances ci"
                " JOIN review_status rs ON rs.entity_type='call_instance'"
                " AND rs.entity_id=ci.id AND rs.tenant_id=ci.tenant_id"
                " WHERE ci.version_id=? AND rs.status='conflict'",
                (version_id,),
            ).fetchone()[0]
        )
    mentions = _rows(
        conn,
        """SELECT l.id, l.document_id, l.api_entity_id, l.match_kind,
                  ae.qualified_name, d.url
           FROM doc_api_links l
           JOIN api_entities ae ON ae.id=l.api_entity_id AND ae.version_id=?
           JOIN documents d ON d.id=l.document_id AND d.version_id=?
           WHERE l.version_id=? AND l.match_kind IN ('symbol','signature')
           ORDER BY l.id LIMIT 12""",
        (version_id, version_id, version_id),
    )
    pattern_next = []
    for row in conn.execute(
        "SELECT id, pattern, support FROM seq_patterns"
        " WHERE version_id=? ORDER BY support DESC, id LIMIT 60",
        (version_id,),
    ):
        names = json.loads(str(row[1]))
        if len(names) < 2:
            continue
        found = []
        for name in (names[0], names[1]):
            entity = conn.execute(
                "SELECT id FROM api_entities"
                " WHERE version_id=? AND qualified_name=? LIMIT 1",
                (version_id, str(name)),
            ).fetchone()
            if entity is not None:
                found.append(int(entity[0]))
        if len(found) == 2:
            pattern_next.append(
                {
                    "row_id": int(row[0]),
                    "source": str(names[0]),
                    "target": str(names[1]),
                    "source_id": found[0],
                    "target_id": found[1],
                    "support": int(row[2]),
                }
            )
        if len(pattern_next) >= 12:
            break
    coverage = _rows(
        conn,
        """SELECT c.id, c.document_id, c.covered_kind, c.support, d.url
           FROM doc_pattern_coverage c JOIN documents d ON d.id=c.document_id
           WHERE c.version_id=? AND d.version_id=?
           ORDER BY c.support DESC, c.id LIMIT 12""",
        (version_id, version_id),
    )
    members = []
    for row in conn.execute(
        "SELECT id, member, cluster_id FROM api_clusters"
        " WHERE version_id=? ORDER BY id LIMIT 200",
        (version_id,),
    ):
        entity = conn.execute(
            "SELECT id FROM api_entities WHERE version_id=? AND qualified_name=? LIMIT 1",
            (version_id, str(row[1])),
        ).fetchone()
        if entity is not None:
            members.append(
                {
                    "row_id": int(row[0]),
                    "entity_id": int(entity[0]),
                    "qualified_name": str(row[1]),
                    "cluster_id": int(row[2]),
                }
            )
        if len(members) >= 12:
            break
    parents = _rows(
        conn,
        """SELECT id, path, parent FROM feature_nodes
           WHERE version_id=? AND parent != ''
             AND EXISTS (SELECT 1 FROM feature_nodes p
                         WHERE p.version_id=? AND p.path=parent)
           ORDER BY id LIMIT 12""",
        (version_id, version_id),
    )
    doc_links = _rows(
        conn,
        """SELECT id, src_url, dst_url, kind FROM doc_link_edges
           WHERE version_id=?
             AND EXISTS (SELECT 1 FROM documents s
                         WHERE s.version_id=? AND s.url=src_url)
           ORDER BY id LIMIT 12""",
        (version_id, version_id),
    )
    if not doc_only:
        for row in conn.execute(
            "SELECT id, qualified_name, bases FROM api_entities"
            " WHERE version_id=? AND bases != '[]' ORDER BY id LIMIT 200",
            (version_id,),
        ):
            try:
                bases = json.loads(str(row[2]))
            except ValueError:
                continue
            for base in bases[:2]:
                resolved = conn.execute(
                    "SELECT count(*) FROM api_entities"
                    " WHERE version_id=? AND qualified_name=?",
                    (version_id, str(base)),
                ).fetchone()[0]
                inherit.append(
                    {
                        "entity_id": int(row[0]),
                        "qualified_name": str(row[1]),
                        "base": str(base),
                        "resolvable": resolved == 1,
                        "row_id": int(row[0]),
                    }
                )
    return {
        "version_id": version_id,
        "profile_id": profile_id,
        "call_inbound": inbound,
        "call_outbound": outbound,
        "import_targets": imports,
        "mentions": mentions,
        "pattern_next": pattern_next,
        "coverage": coverage,
        "members": members,
        "parents": parents,
        "doc_links": doc_links,
        "inherit": inherit[:20],
        "review_excluded_calls": excluded_calls,
    }


def render(
    inventory: dict[str, Any], *, library: str, version: str, doc_only: bool = False
) -> str:
    """Render a compact review note from the evidence inventory."""
    lines = [
        "# Graph gold evidence candidates",
        "",
        f"- scope: {library} {version}",
        f"- version_id: {inventory['version_id']}",
        f"- profile_id: {inventory['profile_id']}",
    ]
    if not doc_only:
        lines.append(f"- conflict-excluded calls: {inventory['review_excluded_calls']}")
    lines.extend(["", "## Call candidates", "", "| Direction | Entity | ID | Calls | First call row |", "|---|---|---:|---:|---:|"])
    if not doc_only:
        for direction, key in (("in", "call_inbound"), ("out", "call_outbound")):
            for row in inventory[key]:
                lines.append(
                    f"| {direction} | {row['qualified_name']} | {row['entity_id']} |"
                    f" {row['calls']} | {row['first_call_id']} |"
                )
    lines.extend(
        [
            "",
            "## Import targets",
            "",
            "| Target | Importers | First row |",
            "|---|---:|---:|",
        ]
    )
    if not doc_only:
        for row in inventory["import_targets"]:
            lines.append(
                f"| {row['target']} | {row['importers']} | {row['first_import_id']} |"
            )
    lines.extend(
        [
            "",
            "## Document candidates",
            "",
            "| Relation | Source | Target | Row |",
            "|---|---|---|---:|",
        ]
    )
    for row in inventory["mentions"]:
        lines.append(
            f"| mentions | {row['url']} | {row['qualified_name']} | {row['id']} |"
        )
    for row in inventory["pattern_next"]:
        lines.append(
            f"| pattern_next | {row['source']} | {row['target']} | {row['row_id']} |"
        )
    for row in inventory["coverage"]:
        lines.append(
            f"| covers_pattern | {row['url']} | {row['covered_kind']} | {row['id']} |"
        )
    for row in inventory["members"]:
        lines.append(
            f"| member | {row['cluster_id']} | {row['qualified_name']} | {row['row_id']} |"
        )
    for row in inventory["parents"]:
        lines.append(
            f"| parent | {row['parent']} | {row['path']} | {row['id']} |"
        )
    for row in inventory["doc_links"]:
        lines.append(
            f"| doc_link | {row['src_url']} | {row['dst_url']} | {row['id']} |"
        )
    if not doc_only:
        lines.extend(
            [
                "",
                "## Inherit candidates",
                "",
                "| Entity | Base | Resolvable |",
                "|---|---|---|",
            ]
        )
        for row in inventory["inherit"]:
            lines.append(
                f"| {row['qualified_name']} | {row['base']} | {row['resolvable']} |"
            )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=ROOT / "tests/e2e/bench/data/bench.db"
    )
    parser.add_argument("--library", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--doc-only", action="store_true")
    parser.add_argument(
        "--out-dir", type=Path, default=ROOT / "tests/e2e/outputs/graph-gold"
    )
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        version_id, profile_id = _scope(
            conn, args.library, args.version, args.tenant_id
        )
        inventory = collect(
            conn, version_id, profile_id, doc_only=args.doc_only
        )
    finally:
        conn.close()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "evidence.json"
    text_path = args.out_dir / "evidence.md"
    json_path.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    text_path.write_text(
        render(
            inventory,
            library=args.library,
            version=args.version,
            doc_only=args.doc_only,
        ),
        encoding="utf-8",
    )
    print(f"graph gold evidence: json={json_path} text={text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
