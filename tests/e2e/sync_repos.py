"""仓库同步：按清单把网络仓拉取到本地持久目录，同步一次多次复用。

用法：uv run python tests/e2e/sync_repos.py [--update] [--lang javascript]
复用现有仓库管理入口的浅克隆与更新语义，不做贮藏等写操作。
网络失败返回码 2 标记环境不可用，不视为回归。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "fixtures" / "manifest.json"
ROOT = Path(__file__).resolve().parent.parent.parent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sync e2e network repos to local dir")
    parser.add_argument("--update", action="store_true", help="已存在仓库时拉取更新")
    parser.add_argument("--lang", action="append", default=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    from libref.astminer.repos import RepoManager
    from libref.db import apply_migrations, open_db

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    langs = args.lang or [k for k, v in manifest.items() if v.get("kind") == "network"]
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "sync.db")
        apply_migrations(conn)
        try:
            manager = RepoManager(ROOT / "tests" / "e2e" / "repos")
            for lang in langs:
                entry = manifest[lang]
                name = f"custom/e2e-{lang}/{entry['tag']}"
                try:
                    root, commit = manager.ensure(
                        conn, name, entry["url"], update=args.update
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"SKIP env unavailable for {lang}: {exc}", file=sys.stderr)
                    return 2
                print(f"{lang} tag={entry['tag']} commit={commit} path={root}")
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
