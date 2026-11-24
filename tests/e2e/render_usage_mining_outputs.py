"""Render source-aware usage mining samples for manual review.

Usage: uv run python tests/e2e/render_usage_mining_outputs.py [--out-dir DIR]
Builds a deterministic seed database from fixtures, runs the production mining
and read services, then writes readable reports under tests/e2e/outputs.
No network, vector backend, or language model is used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "usage_mining"
DEFAULT_OUT = HERE / "outputs" / "usage_mining"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="render usage mining review samples")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    return parser.parse_args()


def load_usage_cases() -> dict[str, Any]:
    """Shared fixture loader for the renderer and its review test."""
    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


def _cases() -> dict[str, Any]:
    return load_usage_cases()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _write(out_dir: Path, name: str, body: str) -> None:
    (out_dir / name).write_text(body.rstrip() + "\n", encoding="utf-8")


async def _label_chat(messages: list[dict[str, Any]]) -> str:
    _ = messages
    return json.dumps(
        {
            "name": "usage pattern",
            "summary": "deterministic review label",
            "category_path": ["usage", "fixture"],
            "workflow": "",
            "related_apis": [],
        },
        ensure_ascii=False,
    )


def _seed(conn: sqlite3.Connection, settings: Any, tmp_path: Path, cases: dict):
    from libref.astminer.models import CodeSnippet
    from libref.services.code.code_ingest import index_repo
    from libref.services.code.repo_ids import entity_uid
    from libref.services.code.usage_ingest import ingest_snippets
    from libref.services.registry.libraries import LibraryService

    library, version = str(cases["library"]), str(cases["version"])
    root = tmp_path / "usage-symbol-repo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(
        "class Service:\n"
        "    def create(self, name):\n"
        "        return name\n\n"
        "    def run(self, name):\n"
        "        return self.create(name)\n",
        encoding="utf-8",
    )
    (root / "pkg" / "factory.py").write_text(
        "from pkg.core import Service\n\n\ndef make_service():\n    return Service()\n",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "import pkg.core as core\n"
        "from pkg.factory import make_service\n\n\n"
        "def prepare():\n"
        "    service = make_service()\n"
        "    service.run('b')\n\n\n"
        "def main():\n"
        "    service = make_service()\n"
        "    service.run('a')\n"
        "    core.Service.create('b')\n",
        encoding="utf-8",
    )
    index_repo(conn, library, version, str(root), repos_dir=tmp_path / "repos")
    _library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version
    )
    for target in cases.get("extra_targets", []):
        name = str(target["name"])
        conn.execute(
            "INSERT OR IGNORE INTO api_entities(entity_uid, library_id, version_id,"
            " qualified_name, kind) VALUES(?,?,?,?,?)",
            (
                entity_uid(version_id, name, "function"),
                _library_id,
                version_id,
                name,
                "function",
            ),
        )
    conn.commit()

    for source in cases["usage_sources"]:
        snippets = []
        for item in source["samples"]:
            snippets.append(
                CodeSnippet(
                    sample_id=str(item["id"]),
                    language=str(item["language"]),
                    code=str(item["code"]),
                    origin_kind=str(source["origin_kind"]),
                    origin_location=str(
                        item.get("origin_location") or source.get("source_url") or ""
                    ),
                    origin_start_line=int(item.get("origin_start_line", 0)),
                )
            )
        ingest_snippets(
            conn,
            library,
            version,
            snippets,
            source_key=str(source["key"]),
            source_role=str(source["role"]),
            origin_kind=str(source["origin_kind"]),
            source_url=str(source.get("source_url") or ""),
            commit_sha=str(source.get("commit_sha") or ""),
            selection=source.get("selection") or {},
            target_filter=source.get("target_filter") or {},
            settings=settings,
        )
    return _library_id, version_id


def _profile_rows(conn, version_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT name, source_ids, support_mode, fingerprint FROM mining_profiles"
        " WHERE version_id=? ORDER BY name",
        (version_id,),
    ).fetchall()
    return [
        {
            "name": str(row[0]),
            "source_ids": json.loads(str(row[1])) if row[1] else [],
            "support_mode": str(row[2]),
            "fingerprint": str(row[3] or ""),
        }
        for row in rows
    ]


def _source_rows(conn, version_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT source_key, source_role, origin_kind, source_url, commit_sha,"
        " selection, target_filter FROM code_sources WHERE version_id=? ORDER BY id",
        (version_id,),
    ).fetchall()
    out = []
    for row in rows:
        out.append(
            {
                "source_key": str(row[0]),
                "source_role": str(row[1]),
                "origin_kind": str(row[2]),
                "source_url": str(row[3] or ""),
                "commit_sha": str(row[4] or ""),
                "selection": json.loads(str(row[5])) if row[5] else {},
                "target_filter": json.loads(str(row[6])) if row[6] else {},
            }
        )
    return out


def _names(patterns: list[dict[str, Any]]) -> set[str]:
    return {str(name) for item in patterns for name in item.get("pattern", [])}


def render(conn: sqlite3.Connection, settings: Any, out_dir: Path) -> dict[str, Any]:
    """Seed the review database and render every usage-mining report."""
    cases = _cases()
    library, version = str(cases["library"]), str(cases["version"])
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        _library_id, version_id = _seed(conn, settings, Path(tmp), cases)

        from libref.services.code.mining_read import MiningReadService
        from libref.services.code.mining_refresh import induce_labels, mine_patterns
        from libref.services.code.usage_sources import resolve_profile

        profile_meta: dict[str, dict[str, Any]] = {}
        patterns: dict[str, list[dict[str, Any]]] = {}
        for profile in cases["profiles"]:
            name = str(profile["name"])
            roles = tuple(profile.get("source_roles") or ()) or None
            resolved = resolve_profile(
                conn, version_id=version_id, name=name, source_roles=roles
            )
            profile_meta[name] = {
                "resolved": {
                    key: value
                    for key, value in resolved.items()
                    if key in ("name", "source_ids", "support_mode")
                },
                "roles": list(roles or []),
            }
            patterns[name] = mine_patterns(
                conn,
                library,
                version,
                minsup=2,
                minlen=2,
                profile=name,
                source_roles=roles,
            )
        impl_patterns = mine_patterns(
            conn, library, version, minsup=2, minlen=2, profile="implementation"
        )
        patterns["implementation"] = impl_patterns
        conn.commit()

        _write(out_dir, "01-sources.txt", _dump(_source_rows(conn, version_id)))
        profiles = _profile_rows(conn, version_id)
        _write(
            out_dir,
            "02-profiles.txt",
            _dump({"resolved": profile_meta, "stored": profiles}),
        )
        ordered = ["implementation"] + [
            str(profile["name"]) for profile in cases["profiles"]
        ]
        for idx, name in enumerate(ordered, start=3):
            _write(out_dir, f"{idx:02d}-patterns-{name}.txt", _dump(patterns[name]))

        reader = MiningReadService(conn, settings)
        usage_name = "usage"
        evidence_pattern = next(
            (
                item["pattern"]
                for item in patterns[usage_name]
                if "rte_eth_rx_burst" in item.get("pattern", [])
            ),
            patterns[usage_name][0]["pattern"] if patterns[usage_name] else [],
        )
        evidence = reader.pattern_evidence(
            evidence_pattern,
            library=library,
            version=version,
            limit=10,
            with_snippet=False,
            profile=usage_name,
        )
        _write(out_dir, "10-evidence-usage.txt", _dump(evidence))
        rendered_evidence = "\n\n".join(item["rendered"] for item in evidence)
        _write(out_dir, "11-evidence-usage-rendered.txt", rendered_evidence)

        health = reader.mining_health(library, version, profile=usage_name)
        _write(out_dir, "12-health-usage.txt", _dump(health))

        label_result = asyncio.run(
            induce_labels(
                conn,
                settings,
                library,
                version,
                profile=usage_name,
                chat=_label_chat,
            )
        )
        labels = reader.list_labels(library, version, limit=10, profile=usage_name)
        from libref.services.docs.answer_materials import build_pattern_block

        _write(
            out_dir,
            "13-labels-usage.txt",
            _dump(
                {
                    "result": label_result,
                    "labels": labels,
                    "answer_block": build_pattern_block(labels, 3),
                }
            ),
        )

        expect = cases.get("expect", {})
        impl_names = _names(impl_patterns)
        usage_names = _names(patterns[usage_name])
        official_names = _names(patterns.get("usage-official", []))
        reference_names = _names(patterns.get("usage-reference", []))
        checks = {
            "implementation_only_present": all(
                name in impl_names for name in expect.get("implementation_only", [])
            ),
            "usage_excludes_implementation": all(
                name not in usage_names for name in expect.get("usage_excludes", [])
            ),
            "official_excludes_reference": all(
                name not in official_names
                for name in expect.get("official_excludes", [])
            ),
            "reference_excludes_official": all(
                name not in reference_names
                for name in expect.get("reference_excludes", [])
            ),
            "usage_includes_reference_apis": all(
                name in usage_names for name in expect.get("usage_includes", [])
            ),
            "evidence_has_profile_fingerprint": bool(evidence)
            and all(item.get("profile_fingerprint") for item in evidence),
            "health_has_coverage": all(
                key in health for key in expect.get("health_keys", [])
            ),
            "labels_carry_profile": bool(labels)
            and all(item.get("profile_fingerprint") for item in labels),
        }
        _write(
            out_dir,
            "14-differential.txt",
            _dump(
                {
                    "implementation": sorted(impl_names),
                    "usage": sorted(usage_names),
                    "official": sorted(official_names),
                    "reference": sorted(reference_names),
                    "checks": checks,
                }
            ),
        )
        summary = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "library": library,
            "version": version,
            "sources": len(_source_rows(conn, version_id)),
            "profiles": len(profiles),
            "pattern_sets": sorted(patterns),
            "evidence_groups": len(evidence),
            "labels": len(labels),
            "checks": checks,
        }
        _write(out_dir, "SUMMARY.txt", _dump(summary))
        return summary


def main() -> int:
    from libref.config import LibrefSettings
    from libref.db import apply_migrations, open_db

    args = _parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "usage-mining.db"
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
            render(conn, settings, Path(args.out_dir))
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
