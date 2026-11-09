"""Raw fetch snapshots: files store large objects, over-quota skips are counted, never blocking source-of-truth writes."""

from __future__ import annotations

import gzip
import hashlib
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_segment(text: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in text.strip())
    return cleaned[:64] or "default"


def version_key(ecosystem: str, library: str, version: str) -> str:
    return f"{_safe_segment(ecosystem)}/{_safe_segment(library)}/{_safe_segment(version)}"


def dir_size_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            continue
    return total


class SnapshotStore:
    def __init__(self, root: Path, *, quota_mb: int = 512) -> None:
        self._root = Path(root)
        self._quota_bytes = max(0, int(quota_mb)) * 1024 * 1024

    @property
    def root(self) -> Path:
        return self._root

    def put(
        self,
        ecosystem: str,
        library: str,
        version: str,
        url: str,
        raw: bytes,
        content_type: str | None = None,
    ) -> tuple[str, bool]:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        target_dir = self._root / version_key(ecosystem, library, version)
        try:
            if self._quota_bytes > 0 and dir_size_bytes(self._root) + len(raw) > self._quota_bytes:
                logger.debug("snapshot quota skip url=%s", url)
                return "", False
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{digest}.gz"
            meta = target_dir / f"{digest}.meta"
            target.write_bytes(gzip.compress(raw))
            fetched_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            meta.write_text(url + "\n" + fetched_at + "\n" + (content_type or ""), encoding="utf-8")
            logger.debug("snapshot written url=%s", url)
            return str(target), True
        except OSError as exc:
            logger.debug("snapshot write failed url=%s: %s", url, exc)
            return "", False

    def version_files(self, ecosystem: str, library: str, version: str) -> list[Path]:
        target_dir = self._root / version_key(ecosystem, library, version)
        if not target_dir.is_dir():
            return []
        try:
            return sorted(p for p in target_dir.iterdir() if p.is_file())
        except OSError:
            return []

    def version_snapshots(self, ecosystem: str, library: str, version: str) -> list[Path]:
        return [p for p in self.version_files(ecosystem, library, version) if p.suffix == ".gz"]

    def drop_version(self, ecosystem: str, library: str, version: str) -> int:
        removed = 0
        for path in self.version_files(ecosystem, library, version):
            try:
                path.unlink()
                if path.suffix == ".gz":
                    removed += 1
            except OSError:
                continue
        target_dir = self._root / version_key(ecosystem, library, version)
        try:
            target_dir.rmdir()
        except OSError:
            pass
        logger.info("snapshot drop %s:%s %s removed=%d", ecosystem, library, version, removed)
        return removed
