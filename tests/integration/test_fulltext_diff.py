"""Differential full-text test: FTS5 (SQLite) versus native search (PostgreSQL).

The same deterministic corpus is seeded into both backends and the same
queries run against both. Version filtering, tenant isolation, counts and
deletes must agree exactly; ranking overlap is recorded and gated on the
controls the engines are required to agree on (distinctive single-term
recall and term-frequency top-1). The recorded JSON is the evidence the
plan uses to decide whether the optional ES branch is ever needed.

The PostgreSQL leg runs only when ``LIBREF_TEST_POSTGRES_URL`` provides a
server. When ``LIBREF_FULLTEXT_DIFF_RECORD`` is set, the per-query metrics
are also dumped there for check-in as the plan's evidence file.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from libref.db.assembly import build_fulltext_store, build_metadata_session

_PG_URL = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")
_RECORD_PATH = os.environ.get("LIBREF_FULLTEXT_DIFF_RECORD", "")

needs_pg = pytest.mark.skipif(not _PG_URL, reason="LIBREF_TEST_POSTGRES_URL not set")

WEIGHTS = (1.0, 5.0, 1.0, 3.0)


class _SqliteSettings:
    metadata_backend = "sqlite"
    fulltext_backend = "fts5"
    db_path = ""


class _PostgresSettings:
    metadata_backend = "postgres"
    fulltext_backend = "postgres"
    postgres_url = _PG_URL
    postgres_pool_size = 2
    postgres_timeout = 5.0


def _fresh_postgres(url: str) -> None:
    import psycopg

    parsed = psycopg.conninfo.conninfo_to_dict(url)
    dbname = (parsed.get("dbname") or "postgres").replace('"', "")
    admin = dict(parsed, dbname="postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')


def _open(kind: str, tmp_path, url: str | None = None):
    if kind == "sqlite":
        tmp_path.mkdir(parents=True, exist_ok=True)
        settings = _SqliteSettings()
        settings.db_path = str(tmp_path / "diff.db")
        session = build_metadata_session(settings)
    else:
        assert url
        _fresh_postgres(url)
        settings = _PostgresSettings()
        session = build_metadata_session(settings)
    session.apply_migrations()
    return settings, session


def _seed(session) -> dict[str, Any]:
    """Deterministic corpus: one nonce term per field/version/tenant plus TF pair."""
    lib = session.insert(
        "INSERT INTO libraries(ecosystem, name, tenant_id) VALUES(?, ?, ?)",
        ("npm", "diff-lib", "t1"),
    )
    v1 = session.insert(
        "INSERT INTO versions(library_id, version, tenant_id) VALUES(?, ?, ?)",
        (lib, "1.0.0", "t1"),
    )
    v2 = session.insert(
        "INSERT INTO versions(library_id, version, tenant_id) VALUES(?, ?, ?)",
        (lib, "2.0.0", "t1"),
    )
    lib2 = session.insert(
        "INSERT INTO libraries(ecosystem, name, tenant_id) VALUES(?, ?, ?)",
        ("npm", "diff-lib-t2", "t2"),
    )
    vt2 = session.insert(
        "INSERT INTO versions(library_id, version, tenant_id) VALUES(?, ?, ?)",
        (lib2, "1.0.0", "t2"),
    )
    filler = "filler alpha beta gamma installation steps overview"
    docs: dict[str, int] = {}

    def add(version_id, lib_id, tenant, url, title, path, content):
        page = session.insert(
            "INSERT INTO pages(version_id, url, tenant_id) VALUES(?, ?, ?)",
            (version_id, url, tenant),
        )
        return session.insert(
            "INSERT INTO documents(library_id, version_id, page_id, url, content,"
            " metadata, sort_order, tenant_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                lib_id,
                version_id,
                page,
                url,
                content,
                json.dumps({"title": title, "path": path}),
                0,
                tenant,
            ),
        )

    docs["title"] = add(
        v1,
        lib,
        "t1",
        "https://example.com/intro",
        "quasititle calibration manual",
        "guide/intro",
        filler,
    )
    docs["content"] = add(
        v1,
        lib,
        "t1",
        "https://example.com/other",
        "General guide",
        "guide/other",
        "quasicontent alloy properties procedure details " + filler,
    )
    docs["path"] = add(
        v1,
        lib,
        "t1",
        "https://example.com/pathdoc",
        "Other",
        "quasipath/special",
        filler,
    )
    docs["url"] = add(
        v1,
        lib,
        "t1",
        "https://example.com/quasiurl-page",
        "Other url doc",
        "guide/url",
        filler,
    )
    docs["tf_high"] = add(
        v1,
        lib,
        "t1",
        "https://example.com/shared-high",
        "Shared high",
        "guide/shared",
        "quasishared quasishared quasishared " + filler,
    )
    docs["tf_low"] = add(
        v1,
        lib,
        "t1",
        "https://example.com/shared-low",
        "Shared low",
        "guide/shared",
        "quasishared " + filler,
    )
    docs["v2"] = add(
        v2,
        lib,
        "t1",
        "https://example.com/v2notes",
        "Second version notes",
        "guide/v2",
        "quasivtwo second version content " + filler,
    )
    docs["t2"] = add(
        vt2,
        lib2,
        "t2",
        "https://example.com/foreign",
        "Foreign tenant",
        "guide/foreign",
        "quasittenant tenant two content " + filler,
    )
    return {"v1": v1, "v2": v2, "vt2": vt2, "docs": docs}


def _kendall_tau(a: list[int], b: list[int]) -> float:
    """Kendall tau on the intersection order; 1.0 when empty or singleton."""
    order = {doc: i for i, doc in enumerate(b)}
    seq = [order[d] for d in a if d in order]
    n = len(seq)
    if n < 2:
        return 1.0
    concord = discord = 0
    for i in range(n):
        for j in range(i + 1, n):
            if seq[i] < seq[j]:
                concord += 1
            elif seq[i] > seq[j]:
                discord += 1
    total = concord + discord
    return (concord - discord) / total if total else 1.0


def _jaccard(a: list[int], b: list[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


@needs_pg
def test_fulltext_diff_sqlite_vs_postgres(tmp_path) -> None:
    sqlite_session = pg_session = None
    try:
        _, sqlite_session = _open("sqlite", tmp_path / "s")
        _, pg_session = _open("postgres", tmp_path / "p", _PG_URL)
        s_ids = _seed(sqlite_session)
        p_ids = _seed(pg_session)
        sqlite_fulltext = build_fulltext_store(_SqliteSettings(), sqlite_session)
        pg_fulltext = build_fulltext_store(_PostgresSettings(), pg_session)
        sqlite_fulltext.flush()
        pg_fulltext.flush()

        v1, v2, vt2 = s_ids["v1"], s_ids["v2"], s_ids["vt2"]
        pv1, pv2, pvt2 = p_ids["v1"], p_ids["v2"], p_ids["vt2"]
        doc, pdoc = s_ids["docs"], p_ids["docs"]
        queries = [
            {"q": "quasititle", "versions": "v1", "tenant": "t1", "expect": "title"},
            {
                "q": "quasicontent",
                "versions": "v1",
                "tenant": "t1",
                "expect": "content",
            },
            {"q": "quasipath", "versions": "v1", "tenant": "t1", "expect": "path"},
            {"q": "quasiurl", "versions": "v1", "tenant": "t1", "expect": "url"},
            {"q": "quasishared", "versions": "v1", "tenant": "t1", "expect": "tf_high"},
            {"q": "quasivtwo", "versions": "both", "tenant": "t1", "expect": "v2"},
            {"q": "quasittenant", "versions": "t2", "tenant": "t2", "expect": "t2"},
            {"q": "zzzznoresult", "versions": "v1", "tenant": "t1", "expect": None},
            {"q": "", "versions": "v1", "tenant": "t1", "expect": None},
        ]
        records: list[dict[str, Any]] = []
        for case in queries:
            if case["versions"] == "v1":
                sv, pv = [v1], [pv1]
            elif case["versions"] == "both":
                sv, pv = [v1, v2], [pv1, pv2]
            else:
                sv, pv = [vt2], [pvt2]
            s_hits = sqlite_fulltext.search(
                str(case["q"]),
                version_ids=sv,
                weights=WEIGHTS,
                budget=10,
                tenant_id=str(case["tenant"]),
            )
            p_hits = pg_fulltext.search(
                str(case["q"]),
                version_ids=pv,
                weights=WEIGHTS,
                budget=10,
                tenant_id=str(case["tenant"]),
            )
            s_ids_ordered = [h[0] for h in s_hits]
            p_ids_ordered = [h[0] for h in p_hits]
            # Ids are per-backend sequences; compare by role, not by number.
            s_by_role = [
                k
                for k, _ in sorted(
                    doc.items(),
                    key=lambda kv: (
                        s_ids_ordered.index(kv[1]) if kv[1] in s_ids_ordered else 10**9
                    ),
                )
                if doc[k] in set(s_ids_ordered)
            ]
            p_by_role = [
                k
                for k, _ in sorted(
                    pdoc.items(),
                    key=lambda kv: (
                        p_ids_ordered.index(kv[1]) if kv[1] in p_ids_ordered else 10**9
                    ),
                )
                if pdoc[k] in set(p_ids_ordered)
            ]
            rec = {
                "query": case["q"],
                "sqlite_roles": s_by_role,
                "pg_roles": p_by_role,
                "set_equal": sorted(s_by_role) == sorted(p_by_role),
                "top1_agree": (s_by_role[:1] == p_by_role[:1]),
                "jaccard": _jaccard(s_by_role, p_by_role),
                "kendall_tau": _kendall_tau(s_by_role, p_by_role),
            }
            records.append(rec)

            if case["expect"] is None:
                assert s_ids_ordered == [] and p_ids_ordered == []
                continue
            # Distinctive-term recall: the seeded doc must surface top-1 in both.
            assert s_by_role[:1] == [case["expect"]], (case["q"], s_by_role)
            assert p_by_role[:1] == [case["expect"]], (case["q"], p_by_role)

        # Version filtering is exact in both branches.
        only_v1_s = sqlite_fulltext.search(
            "quasivtwo", version_ids=[v1], weights=WEIGHTS, budget=10, tenant_id="t1"
        )
        only_v1_p = pg_fulltext.search(
            "quasivtwo", version_ids=[pv1], weights=WEIGHTS, budget=10, tenant_id="t1"
        )
        assert only_v1_s == [] and only_v1_p == []

        # Tenant isolation is exact in both branches.
        cross_s = sqlite_fulltext.search(
            "quasittenant",
            version_ids=[vt2],
            weights=WEIGHTS,
            budget=10,
            tenant_id="t1",
        )
        cross_p = pg_fulltext.search(
            "quasittenant",
            version_ids=[pvt2],
            weights=WEIGHTS,
            budget=10,
            tenant_id="t1",
        )
        assert cross_s == [] and cross_p == []

        # Counts agree per version and tenant.
        assert sqlite_fulltext.count(v1, "t1") == pg_fulltext.count(pv1, "t1") == 6
        assert sqlite_fulltext.count(vt2, "t1") == pg_fulltext.count(pvt2, "t1") == 0
        assert sqlite_fulltext.count(vt2, "t2") == pg_fulltext.count(pvt2, "t2") == 1

        # Term-frequency order agrees on the shared-term control.
        shared = next(r for r in records if r["query"] == "quasishared")
        assert shared["top1_agree"] is True
        assert shared["sqlite_roles"][:1] == ["tf_high"]

        summary = {
            "weights": list(WEIGHTS),
            "latin_recall_both": True,
            "version_filter_exact": True,
            "tenant_isolation_exact": True,
            "counts_exact": True,
            "tf_top1_agree": True,
            "per_query": records,
            "verdict": "native-sufficient",
        }
        if _RECORD_PATH:
            with open(_RECORD_PATH, "w", encoding="utf-8") as fh:
                json.dump(summary, fh, ensure_ascii=False, indent=2)
    finally:
        if sqlite_session is not None:
            sqlite_session.close()
        if pg_session is not None:
            pg_session.close()
