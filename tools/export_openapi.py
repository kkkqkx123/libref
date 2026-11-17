#!/usr/bin/env python3
"""Export the web app's OpenAPI document without starting a server.

`webui/scripts/gen-api.mjs` used to require a running backend on 127.0.0.1:8001,
so the snapshot was never actually produced. Building the app object directly
gives the same document offline: routes, response models, and security schemes
all come from `build_web_app`.

Usage:
    python tools/export_openapi.py [--out webui/openapi.snapshot.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


def _default_out() -> Path:
    return Path(__file__).resolve().parent.parent / "webui" / "openapi.snapshot.json"


def build_document() -> dict:
    """Assemble the app and return its OpenAPI document."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from libref.config import LibrefSettings
    from libref.db import apply_migrations, open_db
    from libref.web import build_web_app

    settings = LibrefSettings()
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "openapi.db")
        try:
            apply_migrations(conn)
            app = build_web_app(conn, settings)
            return app.openapi()
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=_default_out())
    args = parser.parse_args()
    document = build_document()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"openapi snapshot: {args.out} ({len(document.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
