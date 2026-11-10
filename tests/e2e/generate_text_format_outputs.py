"""Offline text-format sample generator for manual review.

Reads deterministic fixtures under tests/e2e/fixtures/text_formats, exercises the
current shaping helpers and services, and writes human-readable samples under
tests/e2e/outputs/text_formats. No network, no vector backend, no language model.
Only argument parsing plus service calls; no business logic.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "text_formats"
DEFAULT_OUT = HERE / "outputs" / "text_formats"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="render text-format review samples")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


def _load_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed_docs(conn: sqlite3.Connection, sample: str) -> tuple[int, str]:
    from libref.chunking import Chunk
    from libref.store.document_store import DocumentStore

    conn.execute("INSERT INTO libraries(name) VALUES('docs')")
    conn.execute("INSERT INTO versions(library_id, version) VALUES(1, '1.0')")
    conn.commit()
    url = "https://d.example/guide.html"
    parts = sample.split("## Usage", 1)
    head = parts[0].strip()
    tail = ("## Usage" + parts[1]).strip() if len(parts) > 1 else ""
    chunks = [
        Chunk(
            url=url,
            path="Sample Guide",
            title="Sample Guide",
            content=head,
            sort_order=0,
            source_group_id="sec_0",
            chunk_id="sec_0_embedding_0",
            chunk_index=0,
            total_chunks=2,
        ),
        Chunk(
            url=url,
            path="Sample Guide / Usage",
            title="Usage",
            content=tail,
            sort_order=1,
            source_group_id="sec_1",
            chunk_id="sec_1_embedding_0",
            chunk_index=0,
            total_chunks=1,
        ),
    ]
    DocumentStore(conn).store_page(1, 1, url, "stored", chunks=chunks, raw_content=sample)
    return 1, url


def _seed_evidence(conn: sqlite3.Connection, case: dict) -> None:
    for idx, call in enumerate(case["calls"]):
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_name, args, repo,"
            " caller_file, caller, confidence, library_id, version_id,"
            " line_start, line_end, call_order, start_byte)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"textfmt-{idx}-{call['callee']}",
                call["callee"],
                json.dumps(call.get("args", []), ensure_ascii=False),
                "r",
                case.get("caller_file", "f.py"),
                case.get("caller", "m.f"),
                call.get("confidence", "EXTRACTED"),
                1,
                1,
                call.get("line_start", idx + 1),
                call.get("line_end", idx + 1),
                idx,
                0,
            ),
        )
    conn.commit()


def _write(out_dir: Path, name: str, body: str) -> None:
    (out_dir / name).write_text(body.rstrip() + "\n", encoding="utf-8")


def main() -> int:
    from libref.db import apply_migrations, open_db
    from libref.services.answer_materials import build_citations, build_messages
    from libref.services.libraries import LibraryService
    from libref.services.mining_read import MiningReadService
    from libref.services.pages import PageService
    from libref.text.normalize import normalize_markdown
    from libref.text.snippet import fences_balanced, make_snippet, truncate_material

    args = _parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sample = (FIXTURES / "markdown_sample.md").read_text(encoding="utf-8")
    snippet_cases = _load_json("snippet_cases.json")
    truncate_cases = _load_json("truncate_cases.json")
    evidence_case = _load_json("evidence_cases.json")
    catalog_cases = _load_json("catalog_cases.json")
    assert isinstance(snippet_cases, list)
    assert isinstance(truncate_cases, list)
    assert isinstance(evidence_case, dict)
    assert isinstance(catalog_cases, list)

    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "textfmt.db")
        apply_migrations(conn)
        try:
            _seed_docs(conn, sample)
            _seed_evidence(conn, evidence_case)  # type: ignore[arg-type]
            pages = PageService(conn)
            mining = MiningReadService(conn)
            libraries = LibraryService(conn)

            lines: list[str] = []
            for case in snippet_cases:  # type: ignore[union-attr]
                rendered = make_snippet(
                    case["content"],
                    case["query"],
                    radius=int(case["radius"]),
                    cap=int(case["cap"]),
                )
                lines.append(f"### {case['id']}")
                lines.append(f"query={case['query']}")
                lines.append(rendered)
            _write(out_dir, "01-snippet.txt", "\n".join(lines))

            lines = []
            for case in truncate_cases:  # type: ignore[union-attr]
                rendered = truncate_material(case["body"], int(case["limit"]))
                lines.append(f"### {case['id']}")
                lines.append(f"balanced={fences_balanced(rendered)}")
                lines.append(rendered)
            _write(out_dir, "02-truncate.txt", "\n".join(lines))

            url = "https://d.example/guide.html"
            page = pages.read_page(url)
            assert page is not None
            _write(out_dir, "03-page.txt", page["content"])
            with (out_dir / "03-page-details.json").open("w", encoding="utf-8") as fh:
                json.dump(page["chunk_details"], fh, ensure_ascii=False, indent=2)

            section = pages.read_section(url, "sec_0")
            assert section is not None
            _write(out_dir, "04-section.txt", section["content"])

            window = pages.read_window(url, 1, radius=1)
            assert window is not None
            _write(out_dir, "05-window.txt", window["content"])
            with (out_dir / "05-window-docs.json").open("w", encoding="utf-8") as fh:
                json.dump(window["docs"], fh, ensure_ascii=False, indent=2)

            rows = conn.execute(
                "SELECT id, url FROM documents ORDER BY sort_order"
            ).fetchall()
            results = [
                {
                    "id": rows[0][0],
                    "url": rows[0][1],
                    "title": "Sample Guide",
                    "library": "docs",
                    "version": "1.0",
                    "source_group_id": "sec_0",
                    "chunk_id": "sec_0_embedding_0",
                    "page_id": 1,
                    "sort_order": 0,
                    "snippet": make_snippet(head_chunk, "setup", radius=16, cap=120),
                }
                for head_chunk in [sample[:400]]
            ]
            full_by_id = {int(results[0]["id"]): sample}
            messages = build_messages("how to setup?", results, full_by_id, 1500)
            _write(out_dir, "06-materials.txt", messages[1]["content"])
            with (out_dir / "06-citations.json").open("w", encoding="utf-8") as fh:
                json.dump(build_citations(results), fh, ensure_ascii=False, indent=2)

            evidence = mining.pattern_evidence(
                evidence_case["pattern"], library="docs"  # type: ignore[index]
            )
            with (out_dir / "07-evidence.json").open("w", encoding="utf-8") as fh:
                json.dump(evidence, fh, ensure_ascii=False, indent=2)
            rendered_evidence = "\n".join(item["rendered"] for item in evidence)
            _write(out_dir, "07-evidence.txt", rendered_evidence)

            lines = []
            for case in catalog_cases:  # type: ignore[union-attr]
                lines.append(f"### {case['id']}")
                lines.append(normalize_markdown(case["raw"]))
            _write(out_dir, "08-catalog.txt", "\n".join(lines))
            conn.execute(
                "UPDATE libraries SET description=? WHERE name='docs'",
                (catalog_cases[0]["raw"],),  # type: ignore[index]
            )
            stored = libraries.list_libraries()[0]["description"]

            summary = [
                f"snippet_cases={len(snippet_cases)}",  # type: ignore[arg-type]
                f"truncate_cases={len(truncate_cases)}",  # type: ignore[arg-type]
                f"page_chunks={page['chunks']}",
                f"section_chunks={section['chunks']}",
                f"window_docs={len(window['docs'])}",
                f"evidence_groups={len(evidence)}",
                f"stored_description_chars={len(stored or '')}",
            ]
            _write(out_dir, "SUMMARY.txt", "\n".join(summary))
        finally:
            conn.close()
    print(f"wrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
