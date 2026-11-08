"""审核工作流：待定优先级与终态流转，全部离线。"""

from __future__ import annotations

import pytest

from libref.services.review import ReviewService


_counter = [0]


def _seed_call(conn, callee: str, confidence: str) -> int:
    _counter[0] += 1
    n = _counter[0]
    conn.execute("INSERT OR IGNORE INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute(
        "INSERT OR IGNORE INTO versions(id, library_id, version) VALUES(1, 1, '1.0')"
    )
    cur = conn.execute(
        "INSERT INTO call_instances(call_uid, callee_name, args, repo, caller_file,"
        " line_start, line_end, confidence, library_id, version_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"uid-{callee}-{confidence}-{n}", callee, "[]", "r", "f.py",
         1, 2, confidence, 1, 1),
    )
    call_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO review_status(entity_type, entity_id, status)"
        " VALUES('call_instance', ?, 'pending')",
        (call_id,),
    )
    conn.commit()
    return call_id


def test_pending_orders_ambiguous_first(conn) -> None:
    ids = {
        c: _seed_call(conn, c, c)
        for c in ("EXTRACTED", "INFERRED", "AMBIGUOUS")
    }
    rows = ReviewService(conn).list_pending()
    assert [r["entity_id"] for r in rows] == [
        ids["AMBIGUOUS"], ids["INFERRED"], ids["EXTRACTED"],
    ]
    assert all(r["status"] == "pending" for r in rows)


def test_approve_conflict_reopen_transitions(conn) -> None:
    svc = ReviewService(conn)
    approved = _seed_call(conn, "a", "AMBIGUOUS")
    svc.approve(approved)
    assert all(r["entity_id"] != approved for r in svc.list_pending(limit=100))
    with pytest.raises(ValueError, match="cannot transition"):
        svc.approve(approved)
    conflicted = _seed_call(conn, "b", "AMBIGUOUS")
    svc.mark_conflict(conflicted)
    with pytest.raises(ValueError, match="cannot transition"):
        svc.mark_conflict(conflicted)
    svc.reopen(conflicted)
    assert any(r["entity_id"] == conflicted for r in svc.list_pending(limit=100))
    with pytest.raises(ValueError, match="cannot reopen"):
        svc.reopen(approved)
