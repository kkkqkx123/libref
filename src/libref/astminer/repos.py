"""仓库管理：本地目录直用，远端浅克隆，文件按后缀遍历。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", "vendor", ".venv", "venv", "target"}
)
_SKIP_NAME_PARTS = ("test", "tests")


def _should_skip(rel: str) -> bool:
    lowered = rel.lower()
    for part in rel.split("/"):
        if part in _SKIP_DIRS:
            return True
        if part in _SKIP_NAME_PARTS:
            return True
    if ".min." in lowered:
        return True
    return False


def iter_source_files(root: Path, suffixes: tuple[str, ...]):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in suffixes:
            continue
        rel = path.relative_to(root).as_posix()
        if _should_skip(rel):
            continue
        yield rel


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
        maybe_local = Path(source)
        if maybe_local.is_dir():
            local = maybe_local.resolve()
            commit = "local"
            self._record(conn, name, source, commit, str(local))
            return local, commit
        target = self._base / name
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

        porcelain.clone(url, str(target), depth=1)

    def _pull(self, target: Path) -> None:
        from dulwich import porcelain

        porcelain.pull(str(target))

    def _head_commit(self, target: Path) -> str:
        try:
            from dulwich.repo import Repo

            repo = Repo(str(target))
            head = repo.head()
            try:
                return head.decode() if isinstance(head, bytes) else str(head)
            finally:
                repo.close()
        except Exception:
            return "unknown"

    def _record(
        self, conn: sqlite3.Connection, name: str, url: str,
        commit: str, local_path: str,
    ) -> None:
        conn.execute(
            "INSERT INTO repos(name, url, commit_sha, local_path)"
            " VALUES(?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET url=excluded.url,"
            " commit_sha=excluded.commit_sha, local_path=excluded.local_path",
            (name, url, commit, local_path),
        )
        conn.commit()
