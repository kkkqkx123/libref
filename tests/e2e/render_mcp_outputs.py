"""Render MCP tool payloads to readable files for manual review.

Usage: uv run python tests/e2e/render_mcp_outputs.py [--network] [--out-dir DIR]
Builds the real MCP server over a deterministic seed database, invokes every
registered tool through call_tool, and writes the structured payloads to disk.
No business logic lives here; the script only seeds fixtures and renders results.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEFAULT_OUT = HERE / "outputs" / "mcp"

PAGE_URL = "https://docs.example/guide.html"
PAGE_URL_2 = "https://docs.example/guide/usage.html"
MARKER = "quetzalmcp"

TOOL_ORDER: tuple[str, ...] = (
    "search",
    "answer",
    "read_doc",
    "catalog",
    "graph_query",
    "library_search",
    "web_search",
    "github_search",
    "resolve",
    "doc_probe",
    "repo_probe",
    "repo_dependencies",
    "link_stats",
    "api_insight",
    "pattern_insight",
    "doc_insight",
    "symbol_insight",
    "call_insight",
    "import_insight",
    "mining_health",
    "review",
    "job_show",
    "submit_feedback",
    "request_repo_index",
    "request_docs_index",
    "index_request_status",
    "wait_index_request",
    "list_index_requests",
    "review_index_request",
)


@dataclass(frozen=True)
class Case:
    """One tool invocation recorded for human review."""

    tool: str
    label: str
    args: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]
    principal_name: str
    network: bool = False
    store_as: str | None = None


def _principal(name: str):
    from libref.services.infra.auth import Principal

    principals = {
        "reader": Principal(id=1, name="reader", scopes={"read"}),
        "developer": Principal(
            id=2,
            name="developer",
            roles=["developer"],
            scopes={"read", "feedback:write"},
        ),
        "reviewer": Principal(
            id=3,
            name="reviewer",
            roles=["kb_admin"],
            scopes={"read", "review:read", "review:write", "index:approve"},
        ),
        "requester": Principal(
            id=4,
            name="requester",
            roles=["external"],
            scopes={"read", "index:request"},
        ),
    }
    return principals[name]


@contextlib.contextmanager
def _principal_scope(name: str):
    from libref.services.infra.auth import current_principal

    token = current_principal.set(_principal(name))
    try:
        yield
    finally:
        current_principal.reset(token)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _call(server, name: str, args: dict[str, Any]) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        result = asyncio.run(server.call_tool(name, args))
    except ToolError as exc:
        return {"_is_error": True, **_error_payload(exc)}
    payload = result.structured_content
    out = dict(payload) if isinstance(payload, dict) else {"value": payload}
    out["_is_error"] = bool(getattr(result, "is_error", False))
    return out


def _error_payload(exc: Exception) -> dict[str, Any]:
    """Recover the one-line payload a failed call carries after the runner prefix."""
    text = str(exc)
    _, _, body = text.partition(": ")
    try:
        parsed = json.loads(body)
    except ValueError:
        return {"message": text}
    return parsed if isinstance(parsed, dict) else {"message": text}


async def _label_chat(messages: list[dict[str, Any]]) -> str:
    _ = messages
    return json.dumps(
        {
            "name": "create and run",
            "summary": "create a service then run it",
            "category_path": ["service", "lifecycle"],
            "workflow": "create then run",
            "related_apis": ["pkg.core.Service.create"],
        },
        ensure_ascii=False,
    )


def seed_review_db(
    conn: sqlite3.Connection, tmp_path: Path, settings: Any
) -> dict[str, Any]:
    """Seed deterministic docs, source, registry, and governance rows for rendering."""
    from libref.chunking import Chunk
    from libref.services.code.mining_read import MiningReadService
    from libref.services.code.mining_refresh import induce_labels, mine_patterns
    from libref.services.infra.jobs import JobManager
    from libref.services.registry.libraries import LibraryService
    from libref.services.registry.resolver import write_cache
    from libref.store.document_store import DocumentStore
    from tests.conftest import index_symbol_repo, seed_library_version

    seed_library_version(conn, "docs", "1.0")
    DocumentStore(conn).store_page(
        1,
        1,
        PAGE_URL,
        "stored",
        chunks=[
            Chunk(
                url=PAGE_URL,
                path="Guide",
                title="Guide",
                content=f"hello libref {MARKER} setup body",
                sort_order=0,
                source_group_id="sec_0",
                chunk_id="sec_0_embedding_0",
                chunk_index=0,
                total_chunks=2,
            ),
            Chunk(
                url=PAGE_URL,
                path="Guide / Usage",
                title="Usage",
                content="usage block with a second paragraph",
                sort_order=1,
                source_group_id="sec_1",
                chunk_id="sec_1_embedding_0",
                chunk_index=0,
                total_chunks=1,
            ),
        ],
        raw_content=f"# Guide\n\nhello libref {MARKER} setup body\n\n## Usage\n",
    )
    conn.executemany(
        "INSERT OR IGNORE INTO doc_link_edges"
        " (library_id, version_id, src_url, dst_url, kind) VALUES(?,?,?,?,?)",
        [
            (1, 1, PAGE_URL, PAGE_URL_2, "internal"),
            (1, 1, PAGE_URL, "https://external.example/ref", "external"),
            (1, 1, PAGE_URL_2, PAGE_URL, "internal"),
        ],
    )
    conn.commit()

    index_symbol_repo(conn, tmp_path, "mcp_symbol_repo")
    catalog = LibraryService(conn)
    demo_id, demo_version_id = catalog.ensure_library_version("demo", "0.1")
    mine_patterns(
        conn,
        "demo",
        "0.1",
        minsup=2,
        minlen=2,
        profile="implementation",
    )
    asyncio.run(
        induce_labels(
            conn,
            settings,
            "demo",
            "0.1",
            profile="implementation",
            chat=_label_chat,
        )
    )
    conn.executemany(
        "INSERT OR IGNORE INTO repo_dependencies"
        " (library_id, version_id, ecosystem, name, req, source_file)"
        " VALUES(?,?,?,?,?,?)",
        [
            (demo_id, demo_version_id, "crates", "serde", "1.0", "Cargo.toml"),
            (demo_id, demo_version_id, "go", "github.com/foo/bar", "v1.2.3", "go.mod"),
        ],
    )
    call_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM call_instances WHERE version_id=? ORDER BY id LIMIT 1",
            (demo_version_id,),
        ).fetchall()
    ]
    if call_ids:
        conn.execute(
            "INSERT OR IGNORE INTO review_status(entity_type, entity_id, status)"
            " VALUES('call_instance', ?, 'pending')",
            (call_ids[0],),
        )
    conn.commit()

    fetched_at = time.time()
    write_cache(
        conn,
        ecosystem="pypi",
        name="demo-lib",
        docs_url="https://docs.demo-lib.example/",
        candidates=["https://docs.demo-lib.example/"],
        versions=["1.0"],
        stable="1.0",
        homepage="https://demo-lib.example/",
        repo_url="https://github.com/example/demo-lib",
        fetched_at=fetched_at,
    )
    candidate = {
        "ecosystem": "pypi",
        "name": "demo-lib",
        "version": "1.0",
        "description": "deterministic review fixture",
        "docs_url": "https://docs.demo-lib.example/",
        "candidates": ["https://docs.demo-lib.example/"],
        "homepage": "https://demo-lib.example/",
        "repo_url": "https://github.com/example/demo-lib",
        "popularity": 1.0,
        "updated": "2026-01-01",
        "exact": True,
        "rank": 1,
        "source": "search:pypi",
    }
    conn.executemany(
        "INSERT INTO discovery_cache(ecosystem, query, candidates, fetched_at)"
        " VALUES(?,?,?,?) ON CONFLICT(ecosystem, query) DO UPDATE SET"
        " candidates=excluded.candidates, fetched_at=excluded.fetched_at",
        [
            ("*", "demo-lib", json.dumps([candidate]), fetched_at),
            ("pypi", "demo-lib", json.dumps([candidate]), fetched_at),
            ("web", "python httpx github", json.dumps([candidate]), fetched_at),
            ("github", "httpx", json.dumps([candidate]), fetched_at),
        ],
    )
    job_id = JobManager(conn).submit(
        "docs-review",
        {"url": PAGE_URL},
        tenant_id="default",
        principal_id=1,
    )
    conn.commit()

    mining = MiningReadService(conn, settings)
    patterns = mining.list_patterns("demo", "0.1", limit=10)
    pattern = (
        list(patterns[0]["pattern"])
        if patterns
        else [
            "pkg.core.Service.create",
            "pkg.core.Service.run",
        ]
    )
    return {
        "pattern": pattern,
        "api": pattern[0],
        "apis": pattern[:2],
        "entity_id": call_ids[0] if call_ids else 0,
        "job_id": job_id,
        "page_url": PAGE_URL,
    }


def build_cases(ctx: dict[str, Any]) -> list[Case]:
    """Declare one or more reviewer-facing cases for each registered tool."""
    pattern = ctx["pattern"]
    api = ctx["api"]
    apis = ctx["apis"]
    return [
        Case("search", "balanced", {"query": MARKER, "preset": "balanced"}, "reader"),
        Case("search", "precise", {"query": MARKER, "preset": "precise"}, "reader"),
        Case("search", "empty-query", {"query": "   "}, "reader"),
        Case("answer", "chat-disabled", {"query": MARKER}, "reader"),
        Case("read_doc", "page", {"url": PAGE_URL, "mode": "page"}, "reader"),
        Case(
            "read_doc",
            "window",
            {"url": PAGE_URL, "mode": "window", "sort_order": 1, "radius": 1},
            "reader",
        ),
        Case(
            "read_doc",
            "section",
            {"url": PAGE_URL, "mode": "section", "source_group_id": "sec_0"},
            "reader",
        ),
        Case("read_doc", "empty-url", {"url": "   "}, "reader"),
        Case("catalog", "libraries", {}, "reader"),
        Case("catalog", "versions", {"library": "docs"}, "reader"),
        Case(
            "library_search",
            "cached",
            {"query": "demo-lib", "ecosystem": "pypi"},
            "reader",
        ),
        Case("web_search", "cached", {"query": "python httpx github"}, "reader"),
        Case("github_search", "cached", {"query": "httpx"}, "reader"),
        Case(
            "resolve",
            "docs-cached",
            {"ecosystem": "pypi", "name": "demo-lib", "target": "docs"},
            "reader",
        ),
        Case(
            "resolve",
            "empty-name",
            {"ecosystem": "pypi", "name": "   "},
            "reader",
        ),
        Case(
            "doc_probe",
            "empty-url",
            {"start_url": "   "},
            "reader",
        ),
        Case(
            "doc_probe",
            "live-site",
            {"start_url": "https://example.com/"},
            "reader",
            network=True,
        ),
        Case(
            "repo_probe",
            "local-rejected",
            {"source": "/tmp/local-only"},
            "reader",
        ),
        Case(
            "repo_probe",
            "live-repo",
            {"source": "https://github.com/encode/httpx", "tree_depth": 2},
            "reader",
            network=True,
        ),
        Case(
            "repo_dependencies", "all", {"library": "demo", "version": "0.1"}, "reader"
        ),
        Case(
            "repo_dependencies",
            "filtered",
            {"library": "demo", "version": "0.1", "dep_ecosystem": "crates"},
            "reader",
        ),
        Case("link_stats", "summary", {"library": "docs", "version": "1.0"}, "reader"),
        Case(
            "link_stats",
            "page",
            {"library": "docs", "version": "1.0", "url": PAGE_URL},
            "reader",
        ),
        Case(
            "graph_query",
            "neighbors",
            {
                "entity": "pkg.core.Service.create",
                "library": "demo",
                "version": "0.1",
                "direction": "both",
            },
            "reader",
        ),
        Case(
            "api_insight",
            "usage",
            {"view": "usage", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "api_insight",
            "related",
            {"view": "related", "api": api, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "api_insight",
            "args",
            {"view": "args", "api": api, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "api_insight",
            "common",
            {"view": "common", "apis": apis, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "pattern_insight",
            "patterns",
            {"view": "patterns", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "pattern_insight",
            "evidence",
            {
                "view": "evidence",
                "pattern": pattern,
                "library": "demo",
                "version": "0.1",
            },
            "reader",
        ),
        Case(
            "pattern_insight",
            "tree",
            {"view": "tree", "prefix": "pkg", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "pattern_insight",
            "clusters",
            {"view": "clusters", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "pattern_insight",
            "health",
            {"view": "health", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "pattern_insight",
            "labels",
            {"view": "labels", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "pattern_insight",
            "snippets",
            {
                "view": "snippets",
                "pattern": pattern,
                "library": "demo",
                "version": "0.1",
            },
            "reader",
        ),
        Case(
            "doc_insight",
            "features",
            {"view": "features", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "doc_insight",
            "links",
            {"view": "links", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "doc_insight",
            "coverage",
            {"view": "coverage", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "doc_insight",
            "consistency",
            {"view": "consistency", "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "symbol_insight",
            "detail",
            {"view": "detail", "apis": apis, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "symbol_insight",
            "file",
            {"view": "file", "api": api, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "symbol_insight",
            "children",
            {
                "view": "children",
                "api": "pkg.core.Service",
                "library": "demo",
                "version": "0.1",
            },
            "reader",
        ),
        Case(
            "symbol_insight",
            "signature",
            {"view": "signature", "api": api, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "call_insight",
            "callee",
            {"view": "callee", "api": api, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "call_insight",
            "caller",
            {
                "view": "caller",
                "api": "pkg.app.main",
                "library": "demo",
                "version": "0.1",
            },
            "reader",
        ),
        Case(
            "call_insight",
            "file",
            {"view": "file", "api": api, "library": "demo", "version": "0.1"},
            "reader",
        ),
        Case(
            "import_insight",
            "importers",
            {
                "view": "importers",
                "api": "pkg.core",
                "library": "demo",
                "version": "0.1",
            },
            "reader",
        ),
        Case(
            "import_insight",
            "imports",
            {
                "view": "imports",
                "api": "pkg/app.py",
                "library": "demo",
                "version": "0.1",
            },
            "reader",
        ),
        Case(
            "mining_health", "scoped", {"library": "demo", "version": "0.1"}, "reader"
        ),
        Case("review", "list", {"action": "list"}, "reviewer"),
        Case(
            "review",
            "approve",
            lambda c: {"action": "approve", "entity_id": c["entity_id"]},
            "reviewer",
        ),
        Case("job_show", "list", {}, "reader"),
        Case(
            "job_show",
            "detail",
            lambda c: {"job_id": c["job_id"]},
            "reader",
        ),
        Case(
            "submit_feedback",
            "created",
            {"body": "review fixture feedback", "kind": "general"},
            "developer",
        ),
        Case(
            "request_repo_index",
            "submitted",
            {
                "library": "review-repo",
                "version": "1.0",
                "source": "https://github.com/example/demo-lib.git",
            },
            "requester",
            store_as="repo_request",
        ),
        Case(
            "request_docs_index",
            "submitted",
            {
                "library": "review-docs",
                "version": "1.0",
                "start_url": "https://docs.example/review",
            },
            "requester",
            store_as="docs_request",
        ),
        Case(
            "index_request_status",
            "repo-request",
            lambda c: {"request_id": c["repo_request"]["id"]},
            "requester",
        ),
        Case(
            "wait_index_request",
            "bounded-wait",
            lambda c: {
                "request_id": c["docs_request"]["id"],
                "timeout": 0,
                "poll": 0.01,
            },
            "requester",
        ),
        Case("list_index_requests", "own", {}, "requester"),
        Case(
            "review_index_request",
            "queue",
            {"action": "list"},
            "reviewer",
        ),
        Case(
            "review_index_request",
            "reject",
            lambda c: {
                "action": "reject",
                "request_id": c["docs_request"]["id"],
                "reason": "review fixture duplicate",
            },
            "reviewer",
        ),
    ]


def render(
    conn: sqlite3.Connection,
    settings: Any,
    out_dir: Path,
    *,
    network: bool = False,
) -> dict[str, Any]:
    """Call every visible tool and write one readable file per tool."""
    from libref.mcp_server.server import ALL_MCP_TOOLS, build_mcp_server

    if set(TOOL_ORDER) != set(ALL_MCP_TOOLS):
        raise ValueError("MCP tool inventory changed; update the review order")
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(str(settings.db_path)).parent
    ctx = seed_review_db(conn, work_dir, settings)
    server = build_mcp_server(conn, settings)
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in TOOL_ORDER}
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    cases = build_cases(ctx)
    for case in cases:
        if case.network and not network:
            skipped.append({"tool": case.tool, "case": case.label})
            continue
        raw_args = case.args
        args = raw_args(ctx) if callable(raw_args) else raw_args
        with _principal_scope(case.principal_name):
            payload = _call(server, case.tool, args)
        grouped[case.tool].append(
            {
                "case": case.label,
                "principal": case.principal_name,
                "args": args,
                "result": payload,
            }
        )
        if case.store_as:
            ctx[case.store_as] = payload
        if payload.get("error") and case.label not in (
            "empty-query",
            "empty-url",
            "empty-name",
            "local-rejected",
            "chat-disabled",
        ):
            errors.append(
                {"tool": case.tool, "case": case.label, "error": str(payload["error"])}
            )

    for tool in TOOL_ORDER:
        body = {
            "tool": tool,
            "generated_at": _now(),
            "cases": grouped[tool],
        }
        (out_dir / f"{tool}.txt").write_text(
            json.dumps(body, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    summary = {
        "generated_at": _now(),
        "network": network,
        "tools": len(TOOL_ORDER),
        "cases": len(cases) - len(skipped),
        "skipped_network": skipped,
        "unexpected_errors": errors,
    }
    (out_dir / "SUMMARY.txt").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="render MCP tool outputs for review")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    parser.add_argument("--network", action="store_true")
    args = parser.parse_args()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    import pytest

    from libref.db import apply_migrations, open_db
    from tests.conftest import make_settings

    with tempfile.TemporaryDirectory(prefix="libref-mcp-review-") as tmp_name:
        tmp_path = Path(tmp_name)
        db_path = tmp_path / "mcp-review.db"
        conn = open_db(db_path, check_same_thread=False)
        apply_migrations(conn)
        with pytest.MonkeyPatch.context() as monkeypatch:
            settings = make_settings(
                monkeypatch,
                tmp_path,
                db_path=str(db_path),
                auth_mode="dev",
                discovery_offline=True,
                registry_offline=True,
                induction_enabled=True,
                simvec_dir=str(tmp_path / "simvec"),
            )
            summary = render(
                conn, settings, Path(args.out_dir), network=bool(args.network)
            )
        conn.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
