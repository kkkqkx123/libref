"""Argument parsing for every subcommand."""

from __future__ import annotations

import argparse

from libref.cli.code_commands import (
    cmd_api_insight,
    cmd_index_repo,
    cmd_induce_labels,
    cmd_mine,
    cmd_pattern_insight,
    cmd_repo_deps,
)
from libref.cli.common import _config_parent
from libref.cli.docs_commands import (
    cmd_answer,
    cmd_dump_chunks,
    cmd_ingest,
    cmd_read,
    cmd_refresh,
    cmd_search,
    cmd_smoke,
)
from libref.cli.ops_commands import (
    cmd_config_show,
    cmd_deletions,
    cmd_fts_check,
    cmd_fts_rebuild,
    cmd_init_db,
    cmd_job_show,
    cmd_purge,
    cmd_reconcile,
    cmd_reembed,
    cmd_review,
    cmd_serve_mcp,
    cmd_serve_web,
)
from libref.cli.registry_commands import (
    cmd_catalog,
    cmd_discover,
    cmd_resolve,
    cmd_search_web,
)
from libref.crawler.registry import REGISTRY_MIRRORS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libref",
        description="libref documentation knowledge base",
        parents=[_config_parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init-db",
        help="Initialize the database (apply migrations)",
        parents=[_config_parent],
    ).set_defaults(func=cmd_init_db)

    p_ingest = sub.add_parser(
        "ingest",
        help="Crawl a documentation site and store it",
        parents=[_config_parent],
    )
    p_ingest.add_argument(
        "library",
        nargs="?",
        help="Library name in explicit form, package name in resolved form",
    )
    p_ingest.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Required in explicit form; defaults to the stable version in resolved form",
    )
    p_ingest.add_argument(
        "start_url",
        nargs="?",
        default=None,
        help="Required in explicit form; mutually exclusive with --ecosystem",
    )
    p_ingest.add_argument(
        "--ecosystem",
        default=None,
        help="Resolve by name: pypi|npm|maven|go|crates|nuget; Java coordinates look like group:artifact",
    )
    p_ingest.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Resolve to the newest version including prereleases",
    )
    p_ingest.add_argument(
        "--offline",
        action="store_true",
        help="Read cache only; do not touch the network",
    )
    p_ingest.add_argument(
        "--registry-mirror",
        default=None,
        help=f"Registry mirror preset ({', '.join(sorted(REGISTRY_MIRRORS))}); defaults to the config value",
    )
    p_ingest.add_argument("--max-pages", type=int, default=None)
    p_ingest.add_argument(
        "--prefilter",
        action="store_true",
        help="Prefilter URLs with a liveness HEAD check",
    )
    p_ingest.add_argument(
        "--async",
        dest="run_async",
        action="store_true",
        help="Run via the jobs queue (submit -> in-process worker consumes -> results written back to the jobs table)",
    )
    p_ingest.add_argument(
        "--allow-cross-site", action="store_true", help="Allow crawling across sites"
    )
    p_ingest.add_argument(
        "--allow-subdomains", action="store_true", help="Allow same-domain subdomains"
    )
    p_ingest.add_argument(
        "--path-prefix",
        action="append",
        default=None,
        help="Accept only paths under this prefix; repeatable",
    )
    p_ingest.add_argument(
        "--path-suffix",
        action="append",
        default=None,
        help="Accept only paths with this suffix; repeatable",
    )
    p_ingest.add_argument(
        "--include-pattern",
        action="append",
        default=None,
        help="Regex allowlist; repeatable",
    )
    p_ingest.add_argument(
        "--exclude-pattern",
        action="append",
        default=None,
        help="Regex denylist; repeatable",
    )
    p_ingest.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum depth relative to the start URL",
    )
    p_ingest.add_argument(
        "--render-js",
        action="store_true",
        help="Fall back to the browser for pages that look JS-rendered",
    )
    p_ingest.add_argument(
        "--render-wait-for",
        default=None,
        help="CSS selector the browser waits for before returning HTML",
    )
    p_ingest.add_argument("--render-timeout-ms", type=int, default=None)
    p_ingest.add_argument(
        "--follow-links",
        action="store_true",
        help="Follow in-page links breadth-first within scope budget",
    )
    p_ingest.add_argument(
        "--crawl-query",
        default=None,
        help="Rank crawl targets by URL relevance to this query",
    )
    p_ingest.add_argument("--score-threshold", type=float, default=None)
    p_ingest.add_argument(
        "--bypass-cache",
        action="store_true",
        help="Skip conditional headers and refetch without cache revalidation",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    p_refresh = sub.add_parser(
        "refresh",
        help="Refresh a version's docs and store them",
        parents=[_config_parent],
    )
    p_refresh.add_argument("library")
    p_refresh.add_argument("version")
    p_refresh.add_argument("start_url")
    p_refresh.add_argument("--ecosystem", default=None)
    p_refresh.add_argument("--max-pages", type=int, default=None)
    p_refresh.add_argument("--prefilter", action="store_true")
    p_refresh.add_argument(
        "--allow-cross-site", action="store_true", help="Allow crawling across sites"
    )
    p_refresh.add_argument(
        "--allow-subdomains",
        action="store_true",
        help="Allow same-domain subdomains",
    )
    p_refresh.add_argument(
        "--path-prefix",
        action="append",
        default=None,
        help="Accept only paths under this prefix; repeatable",
    )
    p_refresh.add_argument(
        "--path-suffix",
        action="append",
        default=None,
        help="Accept only paths with this suffix; repeatable",
    )
    p_refresh.add_argument(
        "--include-pattern",
        action="append",
        default=None,
        help="Regex allowlist; repeatable",
    )
    p_refresh.add_argument(
        "--exclude-pattern",
        action="append",
        default=None,
        help="Regex denylist; repeatable",
    )
    p_refresh.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum depth relative to the start URL",
    )
    p_refresh.add_argument("--render-js", action="store_true")
    p_refresh.add_argument("--render-wait-for", default=None)
    p_refresh.add_argument("--render-timeout-ms", type=int, default=None)
    p_refresh.add_argument("--follow-links", action="store_true")
    p_refresh.add_argument("--crawl-query", default=None)
    p_refresh.add_argument("--score-threshold", type=float, default=None)
    p_refresh.add_argument(
        "--bypass-cache",
        action="store_true",
        help="Skip conditional headers and refetch without cache revalidation",
    )
    p_refresh.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_refresh.set_defaults(func=cmd_refresh)

    sub.add_parser(
        "smoke-local",
        help="Local smoke test: full chain against a temporary HTTP site",
        parents=[_config_parent],
    ).set_defaults(func=cmd_smoke)

    p_search = sub.add_parser(
        "search", help="Default hybrid retrieval", parents=[_config_parent]
    )
    p_search.add_argument("query")
    p_search.add_argument("--library", default=None)
    p_search.add_argument("--version", default=None)
    p_search.add_argument("--ecosystem", default=None)
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument(
        "--mode",
        default="hybrid",
        choices=["hybrid", "precise"],
        help="hybrid fused recall by default, precise for exact full-text",
    )
    p_search.add_argument(
        "--expand-window",
        action="store_true",
        help="Expand the surrounding window on hits",
    )
    p_search.add_argument("--window-radius", type=int, default=None)
    p_search.add_argument(
        "--suggest",
        action="store_true",
        help="Suggest unindexed libraries from registries when local hits are empty",
    )
    p_search.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Include prerelease versions when ranking suggestion candidates",
    )
    p_search.set_defaults(func=cmd_search)

    p_discover = sub.add_parser(
        "discover",
        help="Fuzzy discovery across registries (pypi/npm/maven/go/crates/nuget; language aliases accepted)",
        parents=[_config_parent],
    )
    p_discover.add_argument("query")
    p_discover.add_argument("--library", default=None)
    p_discover.add_argument("--ecosystem", default=None)
    p_discover.add_argument("--limit", type=int, default=10)
    p_discover.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Include prerelease versions when ranking candidates",
    )
    p_discover.set_defaults(func=cmd_discover)

    p_search_web = sub.add_parser(
        "search-web",
        help="Explicit web search for library docs or repos; results are unverified",
        parents=[_config_parent],
    )
    p_search_web.add_argument("query")
    p_search_web.add_argument("--limit", type=int, default=10)
    p_search_web.set_defaults(func=cmd_search_web)

    p_answer = sub.add_parser(
        "answer",
        help="Hybrid retrieval plus grounded answer generation",
        parents=[_config_parent],
    )
    p_answer.add_argument("query")
    p_answer.add_argument("--library", default=None)
    p_answer.add_argument("--version", default=None)
    p_answer.add_argument("--ecosystem", default=None)
    p_answer.add_argument("--limit", type=int, default=5)
    p_answer.add_argument(
        "--expand-window",
        action="store_true",
        help="Expand the surrounding window on hits",
    )
    p_answer.add_argument("--window-radius", type=int, default=None)
    p_answer.add_argument(
        "--material-limit",
        type=int,
        default=None,
        help="Answer material truncation length; defaults to the config value",
    )
    p_answer.set_defaults(func=cmd_answer)

    p_read = sub.add_parser(
        "read", help="Read a document by URL", parents=[_config_parent]
    )
    p_read.add_argument("url")
    p_read.add_argument(
        "--mode",
        default="page",
        choices=["page", "window", "section"],
        help="page for full content, window for neighbors, section for whole group",
    )
    p_read.add_argument("--sort-order", type=int, default=None)
    p_read.add_argument("--radius", type=int, default=1)
    p_read.add_argument("--source-group-id", default=None)
    p_read.add_argument("--library", default=None)
    p_read.add_argument("--version", default=None)
    p_read.add_argument("--ecosystem", default=None)
    p_read.set_defaults(func=cmd_read)

    p_mcp = sub.add_parser(
        "serve-mcp",
        help="Start the MCP server (stdio by default)",
        parents=[_config_parent],
    )
    p_mcp.add_argument(
        "--http",
        action="store_true",
        help="Use the Streamable HTTP transport instead (binds loopback only by default)",
    )
    p_mcp.add_argument("--host", default=None, help="HTTP listen address")
    p_mcp.add_argument("--port", type=int, default=None, help="HTTP listen port")
    p_mcp.add_argument("--path", default=None, help="HTTP endpoint path")
    p_mcp.set_defaults(func=cmd_serve_mcp)

    p_web = sub.add_parser(
        "serve-web",
        help="Start the Web server (binds loopback only by default)",
        parents=[_config_parent],
    )
    p_web.add_argument("--host", default=None, help="HTTP listen address")
    p_web.add_argument("--port", type=int, default=None, help="HTTP listen port")
    p_web.set_defaults(func=cmd_serve_web)

    p_repo = sub.add_parser(
        "index-repo",
        help="Index a source repo and persist call instances",
        parents=[_config_parent],
    )
    p_repo.add_argument(
        "library", help="Library name in explicit form, package name in resolved form"
    )
    p_repo.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Required in explicit form; defaults to the stable version in resolved form",
    )
    p_repo.add_argument(
        "source",
        nargs="?",
        default=None,
        help="Remote URL or local path in explicit form; resolved by name by default",
    )
    p_repo.add_argument(
        "--ecosystem",
        default=None,
        help="Resolve by name: pypi|npm|maven|go|crates|nuget; defaults to explicit custom",
    )
    p_repo.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Resolve to the newest version including prereleases",
    )
    p_repo.add_argument(
        "--offline",
        action="store_true",
        help="Read cache only; do not touch the network",
    )
    p_repo.add_argument(
        "--registry-mirror",
        default=None,
        help="Registry mirror preset; defaults to the config value",
    )
    p_repo.add_argument(
        "--update",
        action="store_true",
        help="Pull updates when the repo already exists",
    )
    p_repo.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_repo.set_defaults(func=cmd_index_repo)

    p_deps = sub.add_parser(
        "repo-deps",
        help="List manifest-declared dependencies",
        parents=[_config_parent],
    )
    p_deps.add_argument("library")
    p_deps.add_argument("version")
    p_deps.add_argument("--ecosystem", default=None)
    p_deps.add_argument("--dep-ecosystem", default=None)
    p_deps.set_defaults(func=cmd_repo_deps)

    p_mine = sub.add_parser(
        "mine", help="Mine closed call-sequence patterns", parents=[_config_parent]
    )
    p_mine.add_argument("library")
    p_mine.add_argument("version")
    p_mine.add_argument(
        "--ecosystem",
        default=None,
        help="Ecosystem of the library; by default resolved across ecosystems",
    )
    p_mine.add_argument("--minsup", type=int, default=2)
    p_mine.add_argument(
        "--topn",
        type=int,
        default=None,
        help="Explicit pruning: keep only fully qualified names ranked in the top N by call count",
    )
    p_mine.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="Include pending calls in sequences (by default only explicit and inferred ones are used)",
    )
    p_mine.add_argument("--maxlen", type=int, default=10)
    p_mine.add_argument("--minlen", type=int, default=2)
    p_mine.add_argument("--cluster-threshold", type=float, default=0.15)
    p_mine.add_argument("--cluster-min-size", type=int, default=2)
    p_mine.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_mine.set_defaults(func=cmd_mine)

    p_catalog = sub.add_parser(
        "catalog", help="Browse libraries and versions", parents=[_config_parent]
    )
    p_catalog.add_argument("library", nargs="?", default=None)
    p_catalog.add_argument("--ecosystem", default=None)
    p_catalog.set_defaults(func=cmd_catalog)

    p_induce = sub.add_parser(
        "induce-labels",
        help="Induce pattern labels with chat",
        parents=[_config_parent],
    )
    p_induce.add_argument("library")
    p_induce.add_argument("version")
    p_induce.add_argument("--ecosystem", default=None)
    p_induce.add_argument("--topn", type=int, default=None)
    p_induce.add_argument(
        "--async", dest="run_async", action="store_true", help="Run via the jobs queue"
    )
    p_induce.set_defaults(func=cmd_induce_labels)

    p_api = sub.add_parser(
        "api-insight", help="API usage views", parents=[_config_parent]
    )
    p_api.add_argument("view", choices=["usage", "related", "args", "common"])
    p_api.add_argument("--api", default=None)
    p_api.add_argument("--apis", default=None)
    p_api.add_argument("--library", default=None)
    p_api.add_argument("--version", default=None)
    p_api.add_argument("--ecosystem", default=None)
    p_api.add_argument("--limit", type=int, default=None)
    p_api.set_defaults(func=cmd_api_insight)

    p_pattern = sub.add_parser(
        "pattern-insight", help="Sequence and structure views", parents=[_config_parent]
    )
    p_pattern.add_argument(
        "view",
        choices=[
            "patterns",
            "evidence",
            "tree",
            "clusters",
            "health",
            "labels",
            "snippets",
        ],
    )
    p_pattern.add_argument(
        "--pattern",
        default=None,
        help="Comma-separated sequence for evidence view",
    )
    p_pattern.add_argument(
        "--containing",
        default=None,
        help="Comma-separated APIs that must all be present",
    )
    p_pattern.add_argument("--prefix", default=None)
    p_pattern.add_argument("--library", default=None)
    p_pattern.add_argument("--version", default=None)
    p_pattern.add_argument("--ecosystem", default=None)
    p_pattern.add_argument("--limit", type=int, default=None)
    p_pattern.add_argument("--per-file-limit", type=int, default=None)
    p_pattern.add_argument("--no-dedup", action="store_true")
    p_pattern.set_defaults(func=cmd_pattern_insight)

    p_resolve = sub.add_parser(
        "resolve", help="Resolve a package address", parents=[_config_parent]
    )
    p_resolve.add_argument("name")
    p_resolve.add_argument("--ecosystem", default=None)
    p_resolve.add_argument("--version", default=None)
    p_resolve.add_argument(
        "--target",
        default="docs",
        choices=["docs", "repo"],
    )
    p_resolve.add_argument(
        "--include-prerelease",
        action="store_true",
        help="Resolve to the newest version including prereleases",
    )
    p_resolve.set_defaults(func=cmd_resolve)

    p_dump = sub.add_parser(
        "dump-chunks",
        help="Preview chunking: line ranges, byte offsets, and headings per file",
        parents=[_config_parent],
    )
    group = p_dump.add_mutually_exclusive_group(required=True)
    group.add_argument("--markdown", default=None, help="Markdown document path")
    group.add_argument("--source", default=None, help="Source file path")
    p_dump.add_argument(
        "--url", default=None, help="URL annotation for document chunks"
    )
    p_dump.set_defaults(func=cmd_dump_chunks)

    p_reembed = sub.add_parser(
        "reembed",
        help="Fully re-embed a version's vectors from the source of truth",
        parents=[_config_parent],
    )
    p_reembed.add_argument("library")
    p_reembed.add_argument("version")
    p_reembed.add_argument("--ecosystem", default=None)
    p_reembed.set_defaults(func=cmd_reembed)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="Repair a version's vectors from the source of truth by set difference",
        parents=[_config_parent],
    )
    p_reconcile.add_argument("library")
    p_reconcile.add_argument("version")
    p_reconcile.add_argument("--ecosystem", default=None)
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_fts_rebuild = sub.add_parser(
        "fts-rebuild",
        help="Rebuild the full-text index from the source tables",
        parents=[_config_parent],
    )
    p_fts_rebuild.set_defaults(func=cmd_fts_rebuild)

    p_fts_check = sub.add_parser(
        "fts-check",
        help="Check full-text index row counts and triggers",
        parents=[_config_parent],
    )
    p_fts_check.set_defaults(func=cmd_fts_check)

    p_review = sub.add_parser(
        "review", help="Review pending call instances", parents=[_config_parent]
    )
    review_sub = p_review.add_subparsers(dest="review_action", required=True)
    review_sub.add_parser("list", help="List pending items").add_argument(
        "--limit",
        type=int,
        default=50,
    )
    for _name in ("approve", "conflict", "reopen"):
        _p = review_sub.add_parser(_name)
        _p.add_argument("entity_id", type=int)
    p_review.set_defaults(func=cmd_review)

    p_job = sub.add_parser(
        "job-show", help="Inspect job audit outcomes", parents=[_config_parent]
    )
    p_job.add_argument("job_id", type=int, nargs="?", default=None)
    p_job.add_argument("--list", action="store_true", help="List recent jobs")
    p_job.add_argument("--limit", type=int, default=20)
    p_job.add_argument("--full", action="store_true", help="Print the full outcome")
    p_job.set_defaults(func=cmd_job_show)

    p_config = sub.add_parser(
        "config-show",
        help="Print the effective configuration, redacted",
        parents=[_config_parent],
    )
    p_config.set_defaults(func=cmd_config_show)

    p_purge = sub.add_parser(
        "purge", help="Delete a whole version and log it", parents=[_config_parent]
    )
    p_purge.add_argument("library")
    p_purge.add_argument("version")
    p_purge.add_argument("--ecosystem", default=None)
    p_purge.set_defaults(func=cmd_purge)

    p_del = sub.add_parser(
        "deletions",
        help="View deletion logs / prune expired records",
        parents=[_config_parent],
    )
    p_del.add_argument("--limit", type=int, default=50)
    p_del.add_argument("--full", action="store_true", help="Print full details")
    p_del.add_argument(
        "--prune",
        action="store_true",
        help="Prune expired records per the retention period",
    )
    p_del.add_argument(
        "--ttl-days",
        type=float,
        default=None,
        help="Defaults to the config value deletion_log_ttl_days",
    )
    p_del.set_defaults(func=cmd_deletions)
    return parser
