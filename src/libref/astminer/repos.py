"""Repository management: use local directories directly, shallow-clone remote ones, walk files by extension."""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        ".tox",
        ".eggs",
        "eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pypackages__",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
    }
)

_SKIP_DIR_SUBSTRINGS = ("test", "testing", "__tests__")


def _should_skip(rel: str) -> bool:
    lowered = rel.lower()
    for part in lowered.split("/"):
        if part in _SKIP_DIRS:
            return True
        if part in _SKIP_DIR_SUBSTRINGS:
            return True
    filename = lowered.rsplit("/", 1)[-1]
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return True
    if filename in ("conftest.py", "test.py"):
        return True
    if filename.endswith((".g.py", ".pb2.py", ".pb2_grpc.py")):
        return True
    return ".min." in lowered


def iter_source_files(
    root: Path,
    suffixes: tuple[str, ...],
    *,
    max_file_bytes: int = 1_000_000,
):
    found: list[str] = []
    for path in root.rglob("*"):
        try:
            if path.is_symlink():
                continue
        except OSError:
            continue
        if not path.is_file():
            continue
        if path.suffix not in suffixes:
            continue
        rel = path.relative_to(root).as_posix()
        if _should_skip(rel):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        found.append(rel)
    found.sort()
    yield from found


class RepoManager:
    def __init__(self, base_dir: Path):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    def ensure(
        self,
        conn: sqlite3.Connection,
        name: str,
        source: str,
        *,
        update: bool = False,
    ) -> tuple[Path, str]:
        if not source:
            raise ValueError("empty repo source")
        maybe_local = Path(source)
        if maybe_local.is_dir():
            local = maybe_local.resolve()
            self._record(conn, name, source, "local", str(local))
            return local, "local"
        target = self._base / name
        base_real = self._base.resolve()
        target_real = target.resolve()
        if target_real != base_real and base_real not in target_real.parents:
            raise ValueError(f"invalid repo name: {name!r}")
        if target.is_dir() and (target / ".git").is_dir():
            if update:
                self._pull(target)
            commit = self._head_commit(target)
            self._record(conn, name, source, commit, str(target))
            return target, commit
        self._clone(source, target)
        commit = self._head_commit(target)
        self._record(conn, name, source, commit, str(target))
        return target, commit

    def _clone(self, url: str, target: Path) -> None:
        from dulwich import porcelain

        Path(target).parent.mkdir(parents=True, exist_ok=True)
        porcelain.clone(url, str(target), depth=1)

    def _pull(self, target: Path) -> None:
        from dulwich import porcelain

        porcelain.pull(str(target))

    def _head_commit(self, target: Path) -> str:
        from dulwich.repo import Repo

        repo = Repo(str(target))
        try:
            head = repo.head()
            return head.decode() if isinstance(head, bytes) else str(head)
        finally:
            repo.close()

    def _record(
        self,
        conn: sqlite3.Connection,
        name: str,
        url: str,
        commit: str,
        local_path: str,
    ) -> None:
        conn.execute(
            "INSERT INTO repos(name, url, commit_sha, local_path)"
            " VALUES(?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET url=excluded.url,"
            " commit_sha=excluded.commit_sha, local_path=excluded.local_path",
            (name, url, commit, local_path),
        )
