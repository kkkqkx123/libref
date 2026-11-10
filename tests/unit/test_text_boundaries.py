"""Golden samples for boundary markers: escaping, gaps, fences, evidence."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_xml_attr_escapes_only_mandatory_chars():
    from libref.text.snippet import xml_escape_attr

    assert xml_escape_attr('a&b<"c\'>') == "a&amp;b&lt;&quot;c&apos;&gt;"
    assert xml_escape_attr(None) == ""
    assert xml_escape_attr("plain") == "plain"


def test_snippet_uses_hit_and_gap_markers():
    from libref.text.snippet import make_snippet

    out = make_snippet("hello libref world", "libref", radius=8, cap=120)
    assert "<hit>libref</hit>" in out
    assert ">>>" not in out
    assert out.startswith("<excerpt>")
    assert out.endswith("</excerpt>")


def test_snippet_truncation_emits_gap_marker():
    from libref.text.snippet import make_snippet

    body = "alpha\n" + ("x " * 200) + "\nlibref tail"
    out = make_snippet(body, "tail", radius=8, cap=40)
    assert "<gap/>" in out
    assert "..." not in out.replace("<gap/>", "")


def test_truncate_material_keeps_fences_balanced():
    from libref.text.snippet import fences_balanced, truncate_material

    body = "intro\n```python\nprint(1)\nprint(2)\nprint(3)\n```\n tail words here"
    cut = truncate_material(body, 30)
    assert fences_balanced(cut)
    assert cut.endswith("<gap/>")
    assert "..." not in cut


def test_material_block_is_self_locating_unit():
    from libref.services.answer_materials import build_messages

    item = {
        "id": 7,
        "url": "https://d.example/a.html",
        "title": "Guide",
        "chunk_id": "sec_0_embedding_0",
        "source_group_id": "sec_0",
        "page_id": 1,
        "sort_order": 0,
    }
    msgs = build_messages("q", [item], {7: "body text"}, 100)
    user = msgs[1]["content"]
    assert "Materials:" in user
    assert '<materials count="1">' in user
    assert 'n="1"' in user
    assert 'doc="7"' in user
    assert "body text" in user


def test_pattern_evidence_marks_gaps_and_hits():
    import sqlite3

    from libref.services.mining_read import MiningReadService

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE libraries(id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute(
        "CREATE TABLE versions(id INTEGER PRIMARY KEY, library_id INTEGER, version TEXT)"
    )
    conn.execute(
        "CREATE TABLE call_instances(id INTEGER PRIMARY KEY, version_id INTEGER,"
        " callee_name TEXT, args TEXT, caller_file TEXT, caller TEXT,"
        " confidence TEXT, line_start INTEGER, line_end INTEGER,"
        " call_order INTEGER, start_byte INTEGER)"
    )
    conn.execute(
        "CREATE TABLE review_status(entity_type TEXT, entity_id INTEGER, status TEXT)"
    )
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    calls = [
        ("a.open", 1, 1),
        ("a.noise", 2, 2),
        ("a.skip", 3, 3),
        ("a.write", 4, 4),
    ]
    for idx, (callee, ls, le) in enumerate(calls):
        conn.execute(
            "INSERT INTO call_instances(version_id, callee_name, args, caller_file,"
            " caller, confidence, line_start, line_end, call_order, start_byte)"
            " VALUES(1, ?, '[]', 'f.py', 'm.f', 'EXTRACTED', ?, ?, ?, 0)",
            (callee, ls, le, idx),
        )
    conn.commit()
    svc = MiningReadService(conn)
    rows = svc.pattern_evidence(["a.open", "a.write"], library="docs")
    assert len(rows) == 1
    rendered = rows[0]["rendered"]
    assert "<unit" in rendered and "</unit>" in rendered
    assert 'hit="true"' in rendered
    assert "<gap" in rendered
    assert rows[0]["matched"] == [0, 3]
    conn.close()
