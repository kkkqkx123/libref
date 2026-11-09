"""端到端 runner：同步本地持久仓，全量索引并导出审查报告。

用法：uv run python tests/e2e/run_repo_index.py --out /tmp/e2e-report.txt
先经同步入口备好本地持久仓，再复用仓库索引入口全量解析，
最后导出逐文件审查报告到 tests/e2e/outputs/。
网络失败返回码 2 标记环境不可用，不视为回归。
只做参数解析加服务调用，不写业务逻辑。
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
    parser.add_argument("--update", action="store_true", help="已存在仓库时拉取更新")
    parser.add_argument("--no-export", action="store_true", help="跳过审查报告导出")
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
    from libref.services.ingest import index_repo

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
                        conn, library, entry["tag"], entry["url"],
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
