"""Archive contract for the retrieval benchmark fixtures.

The acquisition plane is disposable and never read again: crawling, cloning and
snapshot bookkeeping happen under a temporary directory that dies with the
fetch run. The fixture plane is the immutable archive and the only input of the
build. This module owns the spec shape, the archive layout, the metadata schema
and the digest verification both sides rely on.

Layout: one directory per project branch, ``fixtures/{project}/{kind}``,
holding the canonical payload plus ``meta.json``. Identity (library, ecosystem,
version) lives in the metadata, not in the path, so moving the archive never
changes what it describes.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SPEC = HERE / "bench.json"
FIXTURES = HERE / "fixtures"

PAGES_FILE = "pages.jsonl.gz"
SOURCE_FILE = "source.tar.gz"
META_FILE = "meta.json"

_IDENTITY_KEYS = ("project", "kind", "library", "ecosystem", "version")

# Acquisition inputs per kind: the fields that shape the corpus. They are stored
# with the archive so a spec edit without re-acquisition is a hard failure.
_RECIPE_KEYS = {
    "docs": ("start_url", "max_pages", "crawl_delay"),
    "code": ("repo_url", "subdir"),
}


class ArchiveError(RuntimeError):
    """The archive is missing, was modified, or disagrees with the spec."""


def load_spec() -> list[dict[str, Any]]:
    """Fixture entries from the spec, validated for identity and unique ids."""
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    entries = [dict(entry) for entry in data["fixtures"]]
    for entry in entries:
        missing = [key for key in _IDENTITY_KEYS if not entry.get(key)]
        if missing:
            raise ArchiveError(f"bench.json fixture lacks {missing}: {entry}")
        if entry["kind"] not in ("docs", "code"):
            raise ArchiveError(f"bench.json unsupported kind: {entry['kind']!r}")
    ids = [fixture_id(entry) for entry in entries]
    if len(ids) != len(set(ids)):
        raise ArchiveError(f"bench.json duplicate fixture ids: {ids}")
    return entries


def fixture_id(entry: dict[str, Any]) -> str:
    return f"{entry['project']}/{entry['kind']}"


def identity(entry: dict[str, Any]) -> dict[str, Any]:
    """The identity fields a fixture archive carries, in one place."""
    return {key: entry[key] for key in _IDENTITY_KEYS}


def recipe(entry: dict[str, Any]) -> dict[str, Any]:
    """Acquisition inputs of one fixture, normalised for storage and comparison."""
    return {key: entry[key] for key in _RECIPE_KEYS[entry["kind"]] if key in entry}


def select(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Spec entries, narrowed to the given fixture ids in spec order."""
    entries = load_spec()
    if not names:
        return entries
    known = {fixture_id(entry): entry for entry in entries}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise ArchiveError(
            f"unknown fixture ids {sorted(unknown)}; known {sorted(known)}"
        )
    wanted = set(names)
    return [entry for entry in entries if fixture_id(entry) in wanted]


def archive_dir(entry: dict[str, Any]) -> Path:
    return FIXTURES / entry["project"] / entry["kind"]


def payload_name(entry: dict[str, Any]) -> str:
    return PAGES_FILE if entry["kind"] == "docs" else SOURCE_FILE


def payload_path(entry: dict[str, Any], meta: dict[str, Any]) -> Path:
    return archive_dir(entry) / str(meta["payload"]["file"])


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def urls_digest(urls: list[str]) -> str:
    """Fingerprint of the crawled URL set, stable against page order."""
    return hashlib.sha256("\n".join(sorted(urls)).encode("utf-8")).hexdigest()


def iter_pages(path: Path):
    """Stream page rows one at a time: the corpus never has to fit in memory."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def pages_content(path: Path) -> tuple[int, str]:
    """Page count plus URL-set digest, computed without holding the bodies."""
    count = 0
    urls: list[str] = []
    for row in iter_pages(path):
        count += 1
        urls.append(str(row["url"]))
    return count, urls_digest(urls)


def write_pages(path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Write the canonical page payload: byte-stable gzip over sorted JSON lines."""
    ordered = sorted(rows, key=lambda row: str(row["url"]))
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as handle,
    ):
        for row in ordered:
            line = json.dumps(row, ensure_ascii=False, sort_keys=True)
            handle.write((line + "\n").encode("utf-8"))
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest_file(path),
    }


def tree_digest(root: Path) -> tuple[str, int]:
    """Content digest over an extracted source tree: sorted paths plus file bytes."""
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), len(files)


def write_source(path: Path, root: Path) -> dict[str, Any]:
    """Pack a checkout into a byte-stable archive of its files, without the git store.

    Sorted entries plus zeroed timestamps and owners keep the digest stable
    across re-acquisition; the commit identity travels in the metadata because
    an unpacked tree carries none. Symlinks are dropped exactly as the index
    walk drops them: packing their target contents would index files the
    production path never sees.
    """
    files: list[Path] = []
    for file in root.rglob("*"):
        if file.is_symlink():
            continue
        if not file.is_file() or ".git" in file.relative_to(root).parts:
            continue
        files.append(file)
    files.sort(key=lambda file: file.relative_to(root).as_posix())
    if not files:
        raise ArchiveError(f"source checkout holds no files: {root}")
    digest = hashlib.sha256()
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as archive,
    ):
        for file in files:
            rel = file.relative_to(root).as_posix()
            data = file.read_bytes()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o755 if os.access(file, os.X_OK) else 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest_file(path),
        "files": len(files),
        "tree_sha256": digest.hexdigest(),
    }


def extract_source(payload: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(payload, "r:gz") as archive:
        archive.extractall(destination, filter="data")
    return destination


def read_meta(entry: dict[str, Any]) -> dict[str, Any]:
    path = archive_dir(entry) / META_FILE
    if not path.is_file():
        raise ArchiveError(
            f"fixture {fixture_id(entry)} is not archived under {archive_dir(entry)}: "
            "run `uv run python -m tests.e2e.bench.fetch` once"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def write_meta(entry: dict[str, Any], meta: dict[str, Any]) -> None:
    path = archive_dir(entry) / META_FILE
    path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def verify(entry: dict[str, Any]) -> dict[str, Any]:
    """Identity and payload digest; content digests are re-checked by the consumer."""
    meta = read_meta(entry)
    for key in _IDENTITY_KEYS:
        if meta.get(key) != entry[key]:
            raise ArchiveError(
                f"fixture {fixture_id(entry)}: archive says {key}={meta.get(key)!r} "
                f"but the spec says {entry[key]!r}; re-acquire the fixture"
            )
    wanted = recipe(entry)
    if meta.get("recipe") != wanted:
        raise ArchiveError(
            f"fixture {fixture_id(entry)}: archived recipe {meta.get('recipe')!r} "
            f"differs from the spec {wanted!r}; re-acquire the fixture"
        )
    directory = archive_dir(entry)
    payload = directory / str(meta["payload"]["file"])
    if not payload.is_file():
        raise ArchiveError(f"fixture {fixture_id(entry)}: payload missing {payload}")
    actual = digest_file(payload)
    expected = str(meta["payload"]["sha256"])
    if actual != expected:
        raise ArchiveError(
            f"fixture {fixture_id(entry)}: payload digest {actual} differs from the "
            f"archived {expected}; the archive was modified, re-acquire it"
        )
    return meta


def set_readonly(directory: Path) -> None:
    """Freeze the archive: the payload and its metadata stop accepting writes."""
    for path in sorted(directory.rglob("*"), reverse=True):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(directory, 0o555)


def discard(directory: Path) -> None:
    """Remove an archive directory that failed to acquire: failure leaves nothing behind."""
    if not directory.exists():
        return
    set_writable(directory)
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    directory.rmdir()


def set_writable(directory: Path) -> None:
    if not directory.exists():
        return
    os.chmod(directory, 0o755)
    for path in sorted(directory.rglob("*")):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)


def missing(names: list[str] | None = None) -> list[str]:
    """Fixture ids whose archive is absent or was modified."""
    absent: list[str] = []
    for entry in select(names):
        try:
            verify(entry)
        except ArchiveError:
            absent.append(fixture_id(entry))
    return absent
