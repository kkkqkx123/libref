"""Sync pinned real repositories for the usage-mining E2E review.

Usage: uv run python tests/e2e/sync_usage_repos.py [--only target|reference]
Clones each tag with a shallow fetch into the shared persistent e2e repos dir and
writes the resolved commits to repos.lock.json. Network failures exit 2 to mark
the environment unavailable; they are not regressions.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MANIFEST = HERE / "fixtures" / "usage_mining" / "repos.json"
LOCK = HERE / "fixtures" / "usage_mining" / "repos.lock.json"
REPO_ROOT = HERE / "repos" / "usage"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sync pinned usage-mining repos")
    parser.add_argument("--only", action="append", choices=["target", "reference"])
    return parser.parse_args()


def _head_commit(root: Path) -> str:
    from dulwich.repo import Repo

    return Repo(str(root)).head().decode("utf-8", "replace")


def _clone(url: str, tag: str, target: Path) -> None:
    from dulwich import porcelain

    target.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(3):
        parent = Path(
            tempfile.mkdtemp(prefix=target.name + ".clone-", dir=target.parent)
        )
        staging = parent / "repo"
        try:
            porcelain.clone(url, str(staging), depth=1, branch=tag)
        except Exception as exc:  # noqa: BLE001 — retries then reports env failure
            last = exc
            shutil.rmtree(parent, ignore_errors=True)
            if attempt >= 2:
                break
            time.sleep(min(0.5 * (2**attempt), 4.0) + random.uniform(0.0, 0.2))
            continue
        staging.replace(target)
        shutil.rmtree(parent, ignore_errors=True)
        return
    raise RuntimeError(f"clone failed for {url}@{tag}: {last}")


def main() -> int:
    args = _parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    wanted = set(args.only or manifest)
    lock: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repos": {},
    }
    for key, entry in manifest.items():
        if key not in wanted:
            continue
        name = str(entry["name"])
        tag = str(entry["tag"])
        target = REPO_ROOT / name / tag
        try:
            if not (target / ".git").is_dir():
                _clone(str(entry["url"]), tag, target)
            commit = _head_commit(target)
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP env unavailable for {key}: {exc}", file=sys.stderr)
            return 2
        rel = target.relative_to(ROOT).as_posix()
        lock["repos"][key] = {
            **entry,
            "commit": commit,
            "path": rel,
        }
        print(f"{key} tag={tag} commit={commit} path={rel}")
    LOCK.write_text(
        json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
