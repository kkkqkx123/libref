"""Run real usage mining against frozen manifests and render quality gates."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from tests.e2e.usage_mining import render as _render
from tests.e2e.usage_mining.config import DEFAULT_OUT
from tests.e2e.usage_mining.facts import _dump, _inputs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run real-repo usage mining review")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


def main() -> int:
    from libref.config import LibrefSettings
    from libref.db import apply_migrations, open_db

    args = _parse_args()
    _inputs()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "usage-real.db"
        conn = open_db(db_path)
        apply_migrations(conn)
        settings = LibrefSettings(
            db_path=str(db_path),
            discovery_offline=True,
            registry_offline=True,
            induction_enabled=True,
            simvec_dir=str(Path(tmp) / "simvec"),
        )
        try:
            summary = _render.render(conn, settings, Path(args.out_dir))
        finally:
            conn.close()
    print(_dump(summary))
    return 0 if all(summary["checks"].values()) else 1
