"""Archive contract for the retrieval benchmark fixtures.

The acquisition plane is disposable and never read again: crawling, cloning and
snapshot bookkeeping happen under a temporary directory that dies with the
fetch run. The fixture plane is the immutable archive and the only input of the
build. This module owns the spec shape, the archive layout, the metadata schema
and the digest verification both sides rely on.

Layout: one directory per project branch, ``fixtures/{project}/{kind}``,
holding the canonical payload plus ``meta.json``. Identity (library, ecosystem,
version) lives in the metadata, not in the path, so moving the archive never
changes what it describes. A source payload holds only the subtree the build
indexes, at its repository-relative location, so the payload digest covers
exactly the corpus.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    "docs": (
        "start_url",
        "max_pages",
        "crawl_delay",
        "path_prefixes",
        "doc_version",
    ),
    "code": ("repo_url", "subdir", "ref"),
}

# Label of a source fixture that follows the default branch: the payload is the
# repository head, not a fixed release, and the label says so.
HEAD_REF = "head"

# Documentation version marker for a site that states no version at all. It is a
# recorded fact, not a guess: the corpus then says "these docs are unversioned".
UNVERSIONED = "unversioned"

# Path segments that name a documentation stream rather than a release.
_VERSION_STREAMS = frozenset(
    {"latest", "stable", "dev", "develop", "main", "master", "next", "current"}
)
_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:\.x)?$", re.IGNORECASE)


class ArchiveError(RuntimeError):
    """The archive is missing, was modified, or disagrees with the spec."""


def load_spec() -> list[dict[str, Any]]:
    """Fixture entries from the spec, validated for identity and unique ids."""
    data = json.loads(SPEC.read_text(encoding="utf-8"))
    entries = [dict(entry) for entry in data["fixtures"]]
    for entry in entries:
        if entry["kind"] not in ("docs", "code"):
            raise ArchiveError(f"bench.json unsupported kind: {entry['kind']!r}")
        if entry["kind"] == "docs" and not str(entry.get("doc_version") or "").strip():
            raise ArchiveError(
                f"bench.json document fixture lacks doc_version: {entry}; "
                f"state the version it documents, or {UNVERSIONED!r}"
            )
        if entry["kind"] == "code":
            # The version of a source fixture is the ref it was taken at, so it
            # is derived instead of written twice.
            if "version" in entry:
                raise ArchiveError(
                    f"bench.json source fixture declares version: {entry}; "
                    "the version follows from 'ref'"
                )
            entry["version"] = ref_label(entry)
        missing = [key for key in _IDENTITY_KEYS if not entry.get(key)]
        if missing:
            raise ArchiveError(f"bench.json fixture lacks {missing}: {entry}")
    ids = [fixture_id(entry) for entry in entries]
    if len(ids) != len(set(ids)):
        raise ArchiveError(f"bench.json duplicate fixture ids: {ids}")
    return entries


def ref_label(entry: dict[str, Any]) -> str:
    """Source fixture label: the pinned ref, or `head` when it follows the branch."""
    ref = str(entry.get("ref") or "").strip()
    return _path_safe(ref) if ref else HEAD_REF


def _path_safe(ref: str) -> str:
    """Refuse refs that would escape or distort the archive path."""
    if any(ch in ref for ch in "/\\~^: ?*[\x00"):
        raise ArchiveError(f"ref is not path safe: {ref!r}")
    if ref in (".", ".."):
        raise ArchiveError(f"ref is not path safe: {ref!r}")
    return ref


def fixture_id(entry: dict[str, Any]) -> str:
    """Fixture id, and with it the archive directory name.

    A source fixture carries its ref in the name so a directory listing shows
    whether the payload is a repository head or a fixed release.
    """
    name = f"{entry['project']}/{entry['kind']}"
    if entry["kind"] == "code":
        name = f"{name}-{ref_label(entry)}"
    return name


def identity(entry: dict[str, Any]) -> dict[str, Any]:
    """The identity fields a fixture archive carries, in one place."""
    return {key: entry[key] for key in _IDENTITY_KEYS}


def recipe(entry: dict[str, Any]) -> dict[str, Any]:
    """Acquisition inputs of one fixture, normalised for storage and comparison."""
    return {key: entry[key] for key in _RECIPE_KEYS[entry["kind"]] if key in entry}


def _version_like(segment: str) -> bool:
    return segment.lower() in _VERSION_STREAMS or bool(_VERSION_TOKEN.match(segment))


def _segments(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.split("/") if part]


def _from_paths(pages: list[dict[str, Any]]) -> dict[str, str] | None:
    """Version segment shared by the crawled pages, e.g. `/en/2.9/` or `/docs/latest/`."""
    if not pages:
        return None
    counts: dict[tuple[int, str], int] = {}
    samples: dict[tuple[int, str], str] = {}
    for page in pages:
        segments = _segments(str(page["url"]))[:3]
        for index, segment in enumerate(segments):
            key = (index, segment)
            counts[key] = counts.get(key, 0) + 1
            samples.setdefault(key, str(page["url"]))
    floor = max(2, -(-len(pages) * 4 // 5))
    for (index, segment), hits in sorted(counts.items()):
        if hits >= floor and _version_like(segment):
            return {
                "value": segment,
                "signal": f"url-path[{index}]",
                "sample_url": samples[(index, segment)],
            }
    return None


def _first_body(pages: list[dict[str, Any]]) -> tuple[str, str]:
    ordered = sorted(
        pages, key=lambda page: (len(_segments(str(page["url"]))), str(page["url"]))
    )
    page = ordered[0]
    return str(page["url"]), str(page.get("body") or "")


def _from_canonical(pages: list[dict[str, Any]]) -> dict[str, str] | None:
    """Version segment of the page the site declares canonical, when it names one."""
    if not pages:
        return None
    from libref.crawler.extract import scrape_html

    url, body = _first_body(pages)
    if not body.strip():
        return None
    canonical = str(scrape_html(body, url).canonical or "")
    for segment in _segments(canonical):
        if _version_like(segment):
            return {
                "value": segment,
                "signal": "canonical",
                "sample_url": canonical,
            }
    return None


def _from_meta(pages: list[dict[str, Any]]) -> dict[str, str] | None:
    """Version the site publishes in metadata, e.g. Docusaurus docsearch version."""
    if not pages:
        return None
    from selectolax.lexbor import LexborHTMLParser

    url, body = _first_body(pages)
    if not body.strip():
        return None
    tree = LexborHTMLParser(body)
    for node in tree.css("meta"):
        name = (
            node.attributes.get("name") or node.attributes.get("property") or ""
        ).lower()
        if name not in ("docsearch:version", "version"):
            continue
        content = (node.attributes.get("content") or "").strip()
        if content:
            return {
                "value": content,
                "signal": f"meta:{name}",
                "sample_url": url,
            }
    return None


def doc_version_evidence(pages: list[dict[str, Any]]) -> dict[str, str]:
    """What the documentation site itself says it documents.

    Evidence is taken in order of reliability: the version segment of the crawled
    paths, the canonical URL of a page, then site-published metadata. When none of
    them states anything the corpus is marked unversioned instead of guessing from
    the registry.
    """
    for find in (_from_paths, _from_canonical, _from_meta):
        found = find(pages)
        if found:
            return found
    return {"value": UNVERSIONED, "signal": "none", "sample_url": ""}


def check_doc_version(declared: str, evidence: dict[str, str]) -> None:
    """Refuse to freeze a corpus that contradicts the version its spec claims."""
    observed = str(evidence.get("value") or UNVERSIONED)
    if declared == observed:
        return
    raise ArchiveError(
        f"doc_version {declared!r} does not match the site, which states "
        f"{observed!r} (signal {evidence.get('signal')}, sample "
        f"{evidence.get('sample_url') or '-'})"
    )


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
    return FIXTURES / fixture_id(entry)


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


def write_source(path: Path, root: Path, *, prefix: str = "") -> dict[str, Any]:
    """Pack a checkout into a byte-stable archive of its files, without the git store.

    Sorted entries plus zeroed timestamps and owners keep the digest stable
    across re-acquisition; the commit identity travels in the metadata because
    an unpacked tree carries none. Symlinks are dropped exactly as the index
    walk drops them: packing their target contents would index files the
    production path never sees.

    `prefix` keeps the entries at their repository-relative location, so a
    subtree archive still yields the same source identities as indexing the
    whole checkout.
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
            rel = prefix + file.relative_to(root).as_posix()
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
