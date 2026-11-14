"""Acquire the benchmark corpus once and freeze it as an immutable archive.

Acquisition is disposable: the documentation site is crawled by the production
ingest service into a throwaway database and a throwaway snapshot store, and the
source repository is cloned by the production repository manager under a
temporary directory. Both disappear as soon as the canonical payload has been
exported, so no production store layout leaks into the fixture tree.

The export is the freeze point. Documents keep their raw bodies (not the cleaned
markdown) so cleaning and chunking changes can be compared on identical input,
plus the version the site itself states (path segment, canonical or metadata) or
`unversioned` when it states nothing; sources are packed as a working tree
without the git store, with the commit carried by the metadata. The archive is verified before it is made read-only,
and `build.py` verifies it again, so a corpus can only change through an
explicit re-acquisition that produces a new revision.

Usage:
    uv run python -m tests.e2e.bench.fetch [--only ID]... [--reacquire]

A network or site failure exits with code 2, marking the environment
unavailable rather than a regression.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from tests.e2e.bench import archive as bench_archive
from tests.e2e.bench.archive import ArchiveError


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="acquire the benchmark fixtures")
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="fixture id such as httpx/docs; defaults to every fixture in the spec",
    )
    parser.add_argument(
        "--reacquire",
        action="store_true",
        help="overwrite an existing archive; this produces a new revision and invalidates the gates",
    )
    return parser.parse_args()


def _snapshot_rows(store_root: Path, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Export the captured raw bodies of one version from the throwaway store."""
    from libref.store.snapshots import version_key

    version_dir = store_root / version_key(
        entry["ecosystem"], entry["library"], entry["version"]
    )
    rows: list[dict[str, Any]] = []
    for meta_path in sorted(version_dir.glob("*.meta")):
        body_path = version_dir / f"{meta_path.stem}.gz"
        if not body_path.is_file():
            raise ArchiveError(f"snapshot body missing for {meta_path}")
        parts = meta_path.read_text(encoding="utf-8").split("\n")
        body = gzip.decompress(body_path.read_bytes())
        rows.append(
            {
                "url": parts[0] if parts else "",
                "fetched_at": parts[1] if len(parts) > 1 else "",
                "content_type": parts[2] if len(parts) > 2 else "",
                "sha256": hashlib.sha256(body).hexdigest(),
                "body": body.decode("utf-8", "replace"),
            }
        )
    if not rows:
        raise ArchiveError(f"no captured bodies under {version_dir}")
    return rows


def _crawl_docs(entry: dict[str, Any]) -> dict[str, Any]:
    """Crawl the documentation site, then export the raw page bodies."""
    from libref.db import apply_migrations, open_db
    from libref.services.docs.ingest import IngestService
    from tests.e2e.bench.fakes import bench_settings

    with tempfile.TemporaryDirectory(prefix="libref-bench-") as tmp:
        root = Path(tmp)
        db_path = root / "acquire.db"
        conn = open_db(db_path)
        apply_migrations(conn)
        try:
            settings = bench_settings(
                db_path,
                snapshot_enabled=True,
                snapshot_dir=root / "snapshots",
                snapshot_quota_mb=0,
                crawl_delay_default=float(entry.get("crawl_delay", 0.2)),
            )
            prefixes = list(entry.get("path_prefixes") or ())
            scope: dict[str, Any] = {}
            if prefixes:
                scope["path_prefixes"] = prefixes
            if entry.get("follow_links"):
                # The sitemap names only the landing page; the stream is
                # discovered by following site links within the prefixes.
                scope["follow_links"] = True
            extra = [str(url) for url in (entry.get("extra_urls") or ())]
            if extra:
                # Seed pages the main-content prune hides: navigation-only
                # links never surface through link following.
                scope["urls"] = extra
            counts = IngestService(settings, conn).scrape_docs(
                entry["library"],
                entry["version"],
                entry["start_url"],
                max_pages=entry.get("max_pages"),
                scope=scope or None,
                ecosystem=entry["ecosystem"],
                fresh=True,
            )
        finally:
            conn.close()
        rows = _snapshot_rows(root / "snapshots", entry)
    stored = int(counts.get("stored", 0))
    if stored != len(rows):
        raise ArchiveError(
            f"export incomplete: {len(rows)} captured bodies for {stored} stored pages"
        )
    urls = [str(row["url"]) for row in rows]
    evidence = bench_archive.doc_version_evidence(rows)
    bench_archive.check_doc_version(str(entry["doc_version"]), evidence)
    content = {
        "pages": len(rows),
        "urls_sha256": bench_archive.urls_digest(urls),
        "doc_version": evidence,
    }
    return {"rows": rows, "content": content}


def _checkout(entry: dict[str, Any], root: Path) -> tuple[Path, str]:
    """Materialise the source repository at the ref the spec names.

    Without a ref the fixture follows the default branch and goes through the
    production repository manager, which is how every other indexing run gets
    its checkout. A pinned ref is a different acquisition: it needs a shallow
    checkout of that ref instead of the branch tip, so it is done here and the
    resolved commit is recorded, which is what makes "the same view of upstream"
    verifiable later.
    """
    name = f"{entry['ecosystem']}/{entry['library']}/{entry['version']}"
    ref = str(entry.get("ref") or "")
    if not ref:
        from libref.astminer.repos import RepoManager
        from libref.db import apply_migrations, open_db

        conn = open_db(root / "acquire.db")
        apply_migrations(conn)
        try:
            return RepoManager(root / "repos").ensure(conn, name, entry["repo_url"])
        finally:
            conn.close()

    from dulwich import porcelain

    target = root / "repos" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        repo = porcelain.clone(str(entry["repo_url"]), str(target), depth=1, branch=ref)
    except Exception as exc:
        raise ArchiveError(
            f"cannot check out ref {ref!r} of {entry['repo_url']}: {exc}"
        ) from exc
    try:
        head = repo.head()
    finally:
        repo.close()
    return target, head.decode()


def _pack_code(entry: dict[str, Any], payload: Path) -> dict[str, Any]:
    """Check out the source repository, then pack the indexed subtree of the checkout.

    Packaging the subtree rather than the whole checkout keeps the payload equal
    to what the build indexes, so the payload digest says something about the
    corpus. The payload is written inside the acquisition scope because the
    checkout is deleted together with the temporary directory it was cloned into.
    """
    with tempfile.TemporaryDirectory(prefix="libref-bench-") as tmp:
        root = Path(tmp)
        checkout, commit = _checkout(entry, root)
        subdir = str(entry.get("subdir") or "")
        tree = checkout / subdir if subdir else checkout
        if not tree.is_dir():
            raise ArchiveError(f"checkout lacks subdir {subdir}: {checkout}")
        stats = bench_archive.write_source(
            payload,
            tree,
            prefix=f"{subdir}/" if subdir else "",
            exclude=tuple(str(p) for p in (entry.get("exclude_globs") or ())),
        )
        content = {"commit": commit, "subdir": subdir}
    return {**stats, **content}


def _require_free(entry: dict[str, Any], reacquire: bool) -> None:
    """Fail before spending a crawl on a fixture that may not be replaced."""
    directory = bench_archive.archive_dir(entry)
    if directory.exists() and not reacquire:
        raise ArchiveError(
            f"fixture {bench_archive.fixture_id(entry)} already archived at "
            f"{directory}; pass --reacquire to replace it (this produces a new "
            "revision and invalidates the gate floors)"
        )


def _place(entry: dict[str, Any], staged: Path) -> Path:
    """Replace the archive with the staged payload and metadata.

    Everything is written and digested in the staging directory first, so an
    acquisition that fails earlier leaves the previous archive untouched.
    """
    directory = bench_archive.archive_dir(entry)
    bench_archive.discard(directory)
    directory.mkdir(parents=True)
    shutil.move(str(staged), str(directory / staged.name))
    return directory


def _meta(entry: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    meta = bench_archive.identity(entry)
    meta["revision"] = stats["sha256"][:12]
    meta["acquired_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["recipe"] = bench_archive.recipe(entry)
    meta["payload"] = {
        "file": stats["file"],
        "bytes": stats["bytes"],
        "sha256": stats["sha256"],
    }
    meta["content"] = {
        key: value
        for key, value in stats.items()
        if key not in ("file", "bytes", "sha256")
    }
    return meta


def _write_staged_meta(stage: Path, meta: dict[str, Any]) -> None:
    (stage / bench_archive.META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _confirm(entry: dict[str, Any], meta: dict[str, Any]) -> None:
    """Re-read the payload and compare against the metadata just written."""
    payload = bench_archive.payload_path(entry, meta)
    if entry["kind"] == "docs":
        pages, urls = bench_archive.pages_content(payload)
        if pages != meta["content"]["pages"] or urls != meta["content"]["urls_sha256"]:
            raise ArchiveError(
                f"fixture {bench_archive.fixture_id(entry)}: exported pages do not "
                "match the recorded content digest"
            )
        return
    with tempfile.TemporaryDirectory(prefix="libref-bench-verify-") as tmp:
        bench_archive.extract_source(payload, Path(tmp) / "tree")
        digest, files = bench_archive.tree_digest(Path(tmp) / "tree")
    if digest != meta["content"]["tree_sha256"] or files != meta["content"]["files"]:
        raise ArchiveError(
            f"fixture {bench_archive.fixture_id(entry)}: exported tree does not "
            "match the recorded content digest"
        )


def acquire(entry: dict[str, Any], reacquire: bool) -> dict[str, Any]:
    _require_free(entry, reacquire)
    directory = bench_archive.archive_dir(entry)
    with tempfile.TemporaryDirectory(prefix="libref-bench-stage-") as stage:
        staged = Path(stage) / bench_archive.payload_name(entry)
        if entry["kind"] == "docs":
            acquired = _crawl_docs(entry)
            stats = bench_archive.write_pages(staged, acquired["rows"])
            stats["pages"] = acquired["content"]["pages"]
            stats["urls_sha256"] = acquired["content"]["urls_sha256"]
            stats["doc_version"] = acquired["content"]["doc_version"]
        else:
            stats = _pack_code(entry, staged)
        meta = _meta(entry, stats)
        _write_staged_meta(Path(stage), meta)
        _place(entry, staged)
        shutil.move(
            str(Path(stage) / bench_archive.META_FILE),
            str(directory / bench_archive.META_FILE),
        )
    try:
        _confirm(entry, meta)
        bench_archive.set_readonly(directory)
        return bench_archive.verify(entry)
    except BaseException:
        bench_archive.discard(directory)
        raise


def main() -> int:
    args = _parse_args()
    try:
        entries = bench_archive.select(args.only)
    except ArchiveError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    for entry in entries:
        name = bench_archive.fixture_id(entry)
        try:
            meta = acquire(entry, args.reacquire)
        except ArchiveError as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:  # noqa: BLE001 -- network or site failure marks the environment unavailable
            print(f"SKIP {name} unavailable: {exc}", file=sys.stderr)
            return 2
        print(
            f"{name}: revision={meta['revision']} payload={meta['payload']['sha256']} "
            f"content={meta['content']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
