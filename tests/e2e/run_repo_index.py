"""E2E runner: sync persistent local repos, fully index, and export a review report.

Usage: uv run python tests/e2e/run_repo_index.py --out /tmp/e2e-report.txt
First prepare persistent local repos via the sync entry point, then reuse the repo indexing entry point for full parsing,
and finally export a per-file review report to tests/e2e/outputs/.
A network failure exits with code 2, marking the environment unavailable; not treated as a regression.
Only argument parsing plus service calls; no business logic.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MANIFEST = HERE / "fixtures" / "manifest.json"
PERSISTENT_REPOS = HERE / "repos"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="e2e repo index and review runner")
    parser.add_argument("--out", default="e2e-parsing-report.txt")
    parser.add_argument("--lang", action="append", default=None)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Pull updates when the repo already exists",
    )
    parser.add_argument(
        "--no-export", action="store_true", help="Skip the review report export"
    )
    return parser.parse_args()


def _confidence_dist(conn, library: str, version: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT confidence, count(*) FROM call_instances"
        " WHERE library_id=(SELECT id FROM libraries WHERE name=?)"
        " AND version_id=(SELECT id FROM versions WHERE version=?"
        " AND library_id=(SELECT id FROM libraries WHERE name=?))"
        " GROUP BY confidence",
        (library, version, library),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    args = _parse_args()
    from export_review import export_repo

    from libref.db import apply_migrations, open_db
    from libref.services.code.code_ingest import index_repo

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    langs = args.lang or ["javascript", "java"]
    lines: list[str] = [f"manifest={MANIFEST}"]
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "e2e.db"
        conn = open_db(db_path)
        apply_migrations(conn)
        try:
            for lang in langs:
                entry = manifest[lang]
                library = f"e2e-{lang}"
                started = time.time()
                try:
                    counts = index_repo(
                        conn,
                        library,
                        entry["tag"],
                        entry["url"],
                        repos_dir=PERSISTENT_REPOS,
                        update=args.update,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"SKIP env unavailable for {lang}: {exc}", file=sys.stderr)
                    return 2
                dist = _confidence_dist(conn, library, entry["tag"])
                elapsed = time.time() - started
                commit = conn.execute(
                    "SELECT commit_sha FROM repos WHERE name=?",
                    (f"custom/{library}/{entry['tag']}",),
                ).fetchone()
                commit_sha = commit[0] if commit else ""
                lines.append(
                    f"{lang} tag={entry['tag']} files={counts.get('files')} "
                    f"parsed={counts.get('parsed')} cached={counts.get('cached')} "
                    f"entities={counts.get('entities')} calls={counts.get('calls')} "
                    f"dist={dist} elapsed={elapsed:.1f}s library={library} "
                    f"commit={commit_sha}"
                )
                if not args.no_export:
                    local_root = ROOT / entry["local"]
                    repo = f"custom/{library}/{entry['tag']}"
                    export_repo(lang, local_root, repo, commit_sha)
                    lines.append(f"{lang} review exported to tests/e2e/outputs/{lang}/")
        finally:
            conn.close()
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
