"""Syntax gate: every Python source file must parse with CPython.

ruff's parser accepts ``except A, B:`` in some releases (0.17.x) while CPython
rejects it, so ``ruff check`` / ``ruff format --check`` alone cannot catch
un-importable source. This test runs as part of ``uv run pytest``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_all_python_sources_parse() -> None:
    failures: list[str] = []
    for folder in ("src", "tests"):
        for path in sorted((_ROOT / folder).rglob("*.py")):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:
                rel = path.relative_to(_ROOT)
                failures.append(f"{rel}:{exc.lineno}: {exc.msg}")
    assert failures == [], "unparsable Python sources:\n" + "\n".join(failures)
