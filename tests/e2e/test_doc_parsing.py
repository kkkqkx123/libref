"""Doc end-to-end verification: scrape fixed URLs, fully persist, assert invariants only.

Blueprint: docs/tests/parsing-verification.md.
Skipped by default; requires LIBREF_RUN_E2E=1 to enable.
A network failure marks the environment unavailable; not treated as a regression.
Reports are written to disk as human-readable text, not persisted to the DB.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.e2e.run_doc_index import export_site

pytestmark = pytest.mark.skipif(
    os.environ.get("LIBREF_RUN_E2E") != "1",
    reason="e2e requires LIBREF_RUN_E2E=1 and real network",
)

DOC_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "doc_manifest.json"
SNAPSHOT_ROOT = Path(__file__).resolve().parent / "snapshots"


def _stored_total(conn, library: str, version: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM documents WHERE version_id IN"
        " (SELECT id FROM versions WHERE version=?"
        " AND library_id=(SELECT id FROM libraries WHERE name=?))",
        (version, library),
    ).fetchone()
    return int(row[0]) if row else 0


def test_e2e_doc_index_invariants(tmp_path, conn):
    from libref.config import LibrefSettings
    from libref.services.ingest import IngestService

    manifest = json.loads(DOC_MANIFEST.read_text(encoding="utf-8"))
    settings = LibrefSettings(
        crawl_delay_default=0.0,
        snapshot_enabled=True,
        snapshot_dir=SNAPSHOT_ROOT,
        snapshot_quota_mb=64,
    )
    service = IngestService(settings, conn)
    report_lines: list[str] = []
    stored_total = 0
    for site in manifest:
        entry = manifest[site]
        library = f"e2e-docs-{site}"
        try:
            counts = service.scrape_docs(
                library, entry["version"], entry["url"],
                max_pages=entry.get("max_pages"),
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"env unavailable for {site}: {type(exc).__name__}: {exc}")
        assert counts.get("failed", 0) == 0
        stored = counts.get("stored", 0)
        stored_total += stored
        second = service.scrape_docs(
            library, entry["version"], entry["url"],
            max_pages=entry.get("max_pages"),
        )
        assert second.get("failed", 0) == 0
        report_lines.append(f"{site} version={entry['version']} counts={counts}")
        report_lines.extend(export_site(conn, library, entry["version"], site))
    baseline = manifest["baseline"]
    assert _stored_total(conn, "e2e-docs-baseline", baseline["version"]) >= 1
    assert stored_total >= 1
    snapshots = list((SNAPSHOT_ROOT / "custom" / "e2e-docs-baseline"
                        / baseline["version"]).glob("*.gz"))
    assert snapshots, "baseline raw snapshots missing for manual review"
    report = tmp_path / "e2e-doc-report.txt"
    report.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    assert report_lines
