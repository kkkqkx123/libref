"""Download the benchmark fixtures with the production acquisition tooling.

Documents are crawled by the real ingest service (robots/sitemap discovery,
per-host backoff, revalidation, main-content extraction, snapshot capture) into a
throwaway database, and the captured raw bodies are exported to a portable page
cache. Source is cloned by the production repository manager.

Everything this script writes lands under `fixtures/`, which is not versioned:
only this script and `bench.json` are. A network failure exits with code 2,
marking the environment unavailable rather than a regression.

Usage:
    uv run python -m tests.e2e.bench.fetch [--docs-only|--code-only] [--refresh]
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "bench.json"
FIXTURES = HERE / "fixtures"
SNAPSHOTS = FIXTURES / "snapshots"
PAGE_CACHE = FIXTURES / "docs" / "pages.jsonl.gz"
REPOS = FIXTURES / "repos"
LOCK = FIXTURES / "lock.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="download benchmark fixtures")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--docs-only", action="store_true")
    group.add_argument("--code-only", action="store_true")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-crawl and re-pull even when the local fixture already exists",
    )
    return parser.parse_args()


def _spec() -> dict:
    return json.loads(SPEC.read_text(encoding="utf-8"))


def _read_lock() -> dict:
    if not LOCK.is_file():
        return {}
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _write_lock(lock: dict) -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _export_pages(entry: dict) -> dict:
    """Export the crawled raw bodies of one library version from the snapshot store."""
    from libref.store.snapshots import version_key

    version_dir = SNAPSHOTS / version_key("custom", entry["library"], entry["version"])
    if not version_dir.is_dir():
        raise RuntimeError(f"no snapshots captured under {version_dir}")
    rows: list[dict] = []
    for meta_path in sorted(version_dir.glob("*.meta")):
        digest = meta_path.stem
        body_path = version_dir / f"{digest}.gz"
        if not body_path.is_file():
            continue
        parts = meta_path.read_text(encoding="utf-8").split("\n")
        url = parts[0] if parts else ""
        fetched_at = parts[1] if len(parts) > 1 else ""
        content_type = parts[2] if len(parts) > 2 else ""
        body = gzip.decompress(body_path.read_bytes())
        rows.append(
            {
                "url": url,
                "content_type": content_type,
                "fetched_at": fetched_at,
                "sha256": hashlib.sha256(body).hexdigest(),
                "body": body.decode("utf-8", "replace"),
            }
        )
    if not rows:
        raise RuntimeError(f"snapshot directory {version_dir} holds no captured bodies")
    rows.sort(key=lambda row: row["url"])
    PAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(PAGE_CACHE, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    urls_digest = hashlib.sha256(
        "\n".join(row["url"] for row in rows).encode("utf-8")
    ).hexdigest()
    return {
        "start_url": entry["start_url"],
        "library": entry["library"],
        "version": entry["version"],
        "pages": len(rows),
        "urls_sha256": urls_digest,
        "page_cache": str(PAGE_CACHE),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def fetch_docs(entry: dict) -> dict:
    """Crawl one documentation site through the production ingest service."""
    from libref.db import apply_migrations, open_db
    from libref.services.docs.ingest import IngestService
    from tests.e2e.bench.fakes import bench_settings

    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "crawl.db")
        apply_migrations(conn)
        try:
            settings = bench_settings(
                Path(tmp) / "crawl.db",
                snapshot_enabled=True,
                snapshot_dir=SNAPSHOTS,
                snapshot_quota_mb=256,
                crawl_delay_default=float(entry.get("crawl_delay", 0.2)),
            )
            counts = IngestService(settings, conn).scrape_docs(
                entry["library"],
                entry["version"],
                entry["start_url"],
                max_pages=entry.get("max_pages"),
                fresh=True,
            )
        finally:
            conn.close()
    info = _export_pages(entry)
    info["counts"] = counts
    return info


def fetch_code(entry: dict) -> dict:
    """Clone the source repository through the production repository manager."""
    from libref.astminer.repos import RepoManager
    from libref.db import apply_migrations, open_db

    name = f"{entry['ecosystem']}/{entry['library']}/{entry['version']}"
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "clone.db")
        apply_migrations(conn)
        try:
            root, commit = RepoManager(REPOS).ensure(conn, name, entry["repo_url"])
        finally:
            conn.close()
    return {
        "repo_url": entry["repo_url"],
        "library": entry["library"],
        "version": entry["version"],
        "subdir": entry.get("subdir", ""),
        "path": str(root),
        "commit": commit,
    }


def main() -> int:
    args = _parse_args()
    spec = _spec()
    lock = _read_lock()
    wanted = [
        name
        for name in ("docs", "code")
        if not (args.docs_only and name != "docs")
        and not (args.code_only and name != "code")
    ]
    for name in wanted:
        if name in lock and not args.refresh:
            print(f"{name}: cached ({LOCK})")
            continue
        try:
            if name == "docs":
                lock[name] = fetch_docs(spec[name])
            else:
                lock[name] = fetch_code(spec[name])
        except Exception as exc:  # noqa: BLE001 -- network or site failure marks the environment unavailable
            print(f"SKIP fixture '{name}' unavailable: {exc}", file=sys.stderr)
            return 2
        _write_lock(lock)
        summary = ", ".join(
            f"{key}={value}" for key, value in lock[name].items() if key != "counts"
        )
        print(f"{name}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
