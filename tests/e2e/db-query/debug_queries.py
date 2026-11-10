"""Offline audit checks for the database query review.

Builds a scratch database, seeds representative rows, prints query plans for
the previously hot queries, and asserts the fixed behaviors. Any failure
exits non-zero. All output is human-readable text for manual review.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


def _plan(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[str]:
    return [
        " | ".join(str(c) for c in r)
        for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    ]


def _check(name: str, cond: bool, detail: str = "") -> None:
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else "")
    )
    if not cond:
        raise SystemExit(f"audit check failed: {name}")


def main() -> int:
    from libref.chunking import Chunk
    from libref.db import apply_migrations, open_db
    from libref.services.code_ingest import _sweep_source_pages
    from libref.services.maintenance import check_fts, rebuild_fts
    from libref.services.mining_read import MiningReadService
    from libref.services.mining_refresh import mine_patterns
    from libref.services.pages import PageService
    from libref.services.review import ReviewService
    from libref.services.search import SearchService
    from libref.store.document_store import DocumentStore

    tmp = Path(tempfile.mkdtemp(prefix="libref-dbq-"))
    conn = open_db(tmp / "audit.db")
    applied = apply_migrations(conn)
    print(f"migrations applied this run: {applied}")
    versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    _check(
        "migrations 0..12 applied",
        set(range(13)) <= versions,
        f"have={sorted(versions)}",
    )

    conn.execute(
        "INSERT INTO libraries(id, ecosystem, name) VALUES(1, 'custom', 'docs')"
    )
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.commit()
    store = DocumentStore(conn)

    # Dual-lane chunks sharing body text stay apart by lane.
    twins = [
        Chunk(
            url="https://d.example/a.html",
            path="G",
            title="T",
            content="quetzaldbq shared body",
            sort_order=0,
            chunk_path="embedding",
            source_group_id="g0",
            chunk_id="e0",
        ),
        Chunk(
            url="https://d.example/a.html",
            path="G",
            title="T",
            content="quetzaldbq shared body",
            sort_order=1,
            chunk_path="bm25",
            source_group_id="g0",
            chunk_id="b0",
        ),
    ]
    first = store.store_page(
        1, 1, "https://d.example/a.html", "stored", chunks=twins, raw_content="v1"
    )
    _check(
        "twin lanes stored apart",
        len(first.inserted_ids) == 2,
        f"ids={first.inserted_ids}",
    )

    # Reordered restore reuses ids without unique violations.
    swapped = [
        Chunk(
            url="https://d.example/a.html",
            path="G",
            title="T",
            content="quetzaldbq shared body",
            sort_order=0,
            chunk_path="bm25",
            source_group_id="g0",
            chunk_id="b0",
        ),
        Chunk(
            url="https://d.example/a.html",
            path="G",
            title="T",
            content="quetzaldbq shared body",
            sort_order=1,
            chunk_path="embedding",
            source_group_id="g0",
            chunk_id="e0",
        ),
    ]
    second = store.store_page(
        1, 1, "https://d.example/a.html", "stored", chunks=swapped, raw_content="v1b"
    )
    _check(
        "swap reorder reuses ids",
        sorted(second.reused_ids) == sorted(first.inserted_ids),
        f"reused={second.reused_ids}",
    )

    # Unchanged refreshes server credentials.
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=twins,
        raw_content="v1",
        etag="e-old",
        last_modified="m-old",
    )
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "unchanged",
        etag="e-new",
        last_modified="m-new",
    )
    page = store.get_page(1, "https://d.example/a.html")
    _check(
        "unchanged updates etag",
        page is not None and page.etag == "e-new",
        f"etag={page.etag if page else None}",
    )

    # Precise scores read higher-is-better, matching the hybrid lane.
    hits = SearchService(conn).search_docs("quetzaldbq")
    _check("precise hits found", len(hits) >= 1, f"count={len(hits)}")
    scores = [h["score"] for h in hits]
    _check(
        "precise scores descending",
        all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)),
        f"scores={scores[:4]}",
    )

    # Decoy version holding many matches: scoped reads stay version-local
    # even with a small truncation.
    from libref.store.retriever import fts_search

    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(2, 1, '2.0')")
    conn.commit()
    for page_no in range(3):
        store.store_page(
            2,
            1,
            f"https://d.example/decoy-{page_no}.html",
            "stored",
            chunks=[
                Chunk(
                    url=f"https://d.example/decoy-{page_no}.html",
                    path=f"D{page_no}",
                    title=f"Decoy {page_no}",
                    content=f"quetzaldbq decoy filler {page_no}-{slot}",
                    sort_order=slot,
                    chunk_path="embedding",
                    source_group_id=f"d{page_no}",
                    chunk_id=f"d{page_no}-{slot}",
                )
                for slot in range(10)
            ],
            raw_content=f"decoy {page_no}",
        )
    scoped_hits = SearchService(conn).search_docs(
        "quetzaldbq", library="docs", version="1.0", limit=2
    )
    _check(
        "scoped search stays in version",
        len(scoped_hits) == 1 and all(h["version"] == "1.0" for h in scoped_hits),
        f"versions={[h['version'] for h in scoped_hits]}",
    )
    scoped_pairs = fts_search(
        conn,
        "quetzaldbq",
        library="docs",
        version="1.0",
        weights=(1.0, 1.0, 1.0, 1.0),
        budget=50,
    )
    scoped_vids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT version_id FROM documents WHERE id IN "
            f"({','.join('?' for _ in scoped_pairs)})",
            [i for i, _ in scoped_pairs],
        ).fetchall()
    }
    _check(
        "scoped match ids stay in version",
        scoped_vids == {1},
        f"vids={sorted(scoped_vids)}",
    )
    traced: list[str] = []
    conn.set_trace_callback(traced.append)
    try:
        fts_search(
            conn,
            "quetzaldbq",
            library="docs",
            version="1.0",
            weights=(1.0, 1.0, 1.0, 1.0),
            budget=5,
        )
    finally:
        conn.set_trace_callback(None)
    scoped_sql = next(s for s in traced if "documents_fts" in s and "MATCH" in s)
    # The trace callback receives the statement with literals expanded,
    # so the plan runs without bound parameters.
    scoped_plan = _plan(conn, scoped_sql)
    print(f"   scoped fts sql: {scoped_sql}")
    for line in scoped_plan:
        print(f"   plan: {line}")
    _check(
        "scoped fts avoids global sort",
        all("ORDER BY" not in line for line in scoped_plan),
        f"plan={scoped_plan}",
    )

    # Mining seed: two scopes, one holding the full pattern.
    calls = [
        ("a.open", "f.py", "m.f", 1, 0, "EXTRACTED"),
        ("a.noise", "f.py", "m.f", 2, 1, "EXTRACTED"),
        ("a.skip", "f.py", "m.f", 3, 2, "AMBIGUOUS"),
        ("a.write", "f.py", "m.f", 4, 3, "EXTRACTED"),
        ("a.open", "g.py", None, 1, 0, "INFERRED"),
    ]
    for idx, (callee, path, caller, ls, order, conf) in enumerate(calls):
        scope = f"{path}\x00{caller or ''}" if idx < 4 else None
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_name, args, repo, caller_file,"
            " line_start, line_end, confidence, library_id, version_id, caller,"
            " receiver, func, argument_count, call_order, start_byte, end_byte,"
            " commit_sha, scope)"
            " VALUES(?, ?, '[]', 'r', ?, ?, ?, ?, 1, 1, ?, '', '', 0, ?, 0, 0, '', ?)",
            (f"u{idx}", callee, path, ls, ls, conf, caller, order, scope),
        )
    conn.commit()
    reader = MiningReadService(conn)
    usage = reader.api_usage_counts(library="docs")
    _check(
        "usage counts cover null-scope fallback",
        any(u["api"] == "a.open" and u["calls"] == 2 for u in usage),
        f"usage={usage[:3]}",
    )

    # Review: lowest confidence first, limit pushed down.
    ids = [
        r[0]
        for r in conn.execute("SELECT id FROM call_instances ORDER BY id").fetchall()
    ]
    for i in ids:
        conn.execute(
            "INSERT INTO review_status(entity_type, entity_id, status) VALUES('call_instance', ?, 'pending')",
            (i,),
        )
    conn.commit()
    pending = ReviewService(conn).list_pending(limit=1)
    _check(
        "pending lowest confidence first",
        len(pending) == 1 and pending[0]["confidence"] == "AMBIGUOUS",
        f"first={pending[0]['confidence'] if pending else None}",
    )

    # Evidence prunes to the matching scope only. The ambiguous call is
    # excluded by default and kept with the inclusive flag.
    groups = reader.pattern_evidence(["a.open", "a.write"], library="docs")
    _check(
        "evidence finds one group",
        len(groups) == 1 and groups[0]["matched"] == [0, 2],
        f"matched={groups[0].get('matched') if groups else None}",
    )
    wide = reader.pattern_evidence(
        ["a.open", "a.write"], library="docs", include_ambiguous=True
    )
    _check(
        "evidence inclusive keeps ambiguous",
        len(wide) == 1 and wide[0]["matched"] == [0, 3],
        f"matched={wide[0].get('matched') if wide else None}",
    )

    # Patterns mine and clear uniformly. A second scope lifts support to 2.
    for idx, callee in enumerate(["a.open", "a.write"]):
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_name, args, repo, caller_file,"
            " line_start, line_end, confidence, library_id, version_id, caller,"
            " receiver, func, argument_count, call_order, start_byte, end_byte,"
            " commit_sha, scope)"
            " VALUES(?, ?, '[]', 'r', 'i.py', ?, ?, 'EXTRACTED', 1, 1, 'm.i',"
            " '', '', 0, ?, 0, 0, '', 'i.py' || char(0) || 'm.i')",
            (f"w{idx}", callee, idx + 1, idx + 1, idx),
        )
    conn.commit()
    mined = mine_patterns(conn, "docs", "1.0", minsup=2)
    _check("patterns mined", len(mined) >= 1, f"count={len(mined)}")

    # Ghost source pages are swept.
    conn.execute(
        "INSERT INTO pages(version_id, url) VALUES(1, 'repo://custom/docs/1.0/old.py')"
    )
    pid = conn.execute(
        "SELECT id FROM pages WHERE url='repo://custom/docs/1.0/old.py'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO documents(library_id, version_id, page_id, url, content, sort_order)"
        " VALUES(1, 1, ?, 'repo://custom/docs/1.0/old.py', 'x', 0)",
        (pid,),
    )
    conn.commit()
    swept = _sweep_source_pages(conn, "custom/docs/1.0", {"new.py"})
    conn.commit()
    left = conn.execute(
        "SELECT count(*) FROM pages WHERE url LIKE 'repo://%'"
    ).fetchone()[0]
    _check("ghost source page swept", left == 0 and len(swept) == 1, f"swept={swept}")

    # Full-text self check passes with the row-targeted probe.
    rebuild_fts(conn)
    health = check_fts(conn)
    _check("fts in sync", health["in_sync"] == 1, f"{health}")

    # Page read excludes the bm25 twin lane like the window path does.
    doc = PageService(conn).read_page("https://d.example/a.html", library="docs")
    _check(
        "page read single lane",
        doc is not None and doc["chunks"] == 1,
        f"chunks={doc['chunks'] if doc else None}",
    )

    print("--- query plans ---")
    for label, sql in [
        (
            "section filter",
            (
                "SELECT d.content FROM documents d WHERE d.url='u'"
                " AND json_extract(d.metadata,'$.source_group_id')='g'"
            ),
        ),
        (
            "pending scan",
            (
                "SELECT rs.entity_id FROM review_status rs"
                " WHERE rs.entity_type='call_instance' AND rs.status='pending'"
                " ORDER BY rs.updated_at, rs.entity_id LIMIT 1"
            ),
        ),
        (
            "url-only page read",
            "SELECT p.id FROM pages p WHERE p.url='https://d.example/a.html'",
        ),
    ]:
        print(f"## {label}\n   sql: {sql}")
        for line in _plan(conn, sql):
            print(f"   plan: {line}")

    conn.close()
    print("all audit checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
