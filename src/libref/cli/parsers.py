"""Argument parser for the server-local operations CLI."""

from __future__ import annotations

import argparse

from libref.cli.common import _config_parent
from libref.cli.ops_commands import (
    cmd_config_show,
    cmd_deletions,
    cmd_fts_check,
    cmd_fts_rebuild,
    cmd_init_db,
    cmd_job_cancel,
    cmd_job_show,
    cmd_purge,
    cmd_reconcile,
    cmd_reembed,
    cmd_review,
    cmd_role_audit,
    cmd_role_split,
    cmd_serve,
    cmd_serve_mcp,
    cmd_smoke,
    cmd_vectors_optimize,
    cmd_vectors_status,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libref",
        description="libref server startup and local maintenance",
        parents=[_config_parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init-db",
        help="Initialize the database (apply migrations)",
        parents=[_config_parent],
    ).set_defaults(func=cmd_init_db)

    sub.add_parser(
        "smoke-local",
        help="Run the local discovery-to-storage smoke test",
        parents=[_config_parent],
    ).set_defaults(func=cmd_smoke)

    p_serve_mcp = sub.add_parser(
        "serve-mcp",
        help="Start the MCP server over stdio",
        parents=[_config_parent],
    )
    p_serve_mcp.add_argument(
        "--enabled-tools",
        default=None,
        help="Comma-separated tool allowlist; defaults to the config value",
    )
    p_serve_mcp.add_argument(
        "--disabled-tools",
        default=None,
        help="Comma-separated tool denylist; defaults to the config value",
    )
    p_serve_mcp.set_defaults(func=cmd_serve_mcp)

    p_serve = sub.add_parser(
        "serve",
        help="Start combined Web plus MCP serving on one port",
        parents=[_config_parent],
    )
    p_serve.add_argument("--host", default=None, help="HTTP listen address")
    p_serve.add_argument("--port", type=int, default=None, help="HTTP listen port")
    p_serve.add_argument(
        "--stdio",
        action="store_true",
        help="Run MCP over stdio instead of combined HTTP serving",
    )
    p_serve.add_argument(
        "--enabled-tools",
        default=None,
        help="Comma-separated MCP tool allowlist for combined serving",
    )
    p_serve.add_argument(
        "--disabled-tools",
        default=None,
        help="Comma-separated MCP tool denylist for combined serving",
    )
    p_serve.set_defaults(func=cmd_serve)

    for name, command, help_text in (
        (
            "reembed",
            cmd_reembed,
            "Re-embed a version's vectors from the source of truth",
        ),
        ("reconcile", cmd_reconcile, "Repair a version's vectors by set difference"),
    ):
        p = sub.add_parser(name, help=help_text, parents=[_config_parent])
        p.add_argument("library")
        p.add_argument("version")
        p.add_argument("--ecosystem", default=None)
        p.set_defaults(func=command)

    for name, command, help_text in (
        ("fts-rebuild", cmd_fts_rebuild, "Rebuild the full-text index"),
        ("fts-check", cmd_fts_check, "Check full-text index consistency"),
    ):
        sub.add_parser(name, help=help_text, parents=[_config_parent]).set_defaults(
            func=command
        )

    for name, command, help_text in (
        (
            "vectors-status",
            cmd_vectors_status,
            "Report vector index health",
        ),
        (
            "vectors-optimize",
            cmd_vectors_optimize,
            "Publish pending vector index maintenance",
        ),
    ):
        sub.add_parser(name, help=help_text, parents=[_config_parent]).set_defaults(
            func=command
        )

    p_review = sub.add_parser(
        "review", help="Review pending call instances locally", parents=[_config_parent]
    )
    review_sub = p_review.add_subparsers(dest="review_action", required=True)
    review_sub.add_parser("list", help="List pending items").add_argument(
        "--limit", type=int, default=50
    )
    for name in ("approve", "conflict", "reopen"):
        review_sub.add_parser(name).add_argument("entity_id", type=int)
    p_review.set_defaults(func=cmd_review)

    p_job = sub.add_parser(
        "job-show", help="Inspect local job audit outcomes", parents=[_config_parent]
    )
    p_job.add_argument("job_id", type=int, nargs="?", default=None)
    p_job.add_argument("--list", action="store_true", help="List recent jobs")
    p_job.add_argument("--limit", type=int, default=20)
    p_job.add_argument("--full", action="store_true", help="Print the full outcome")
    p_job.set_defaults(func=cmd_job_show)

    p_cancel = sub.add_parser(
        "job-cancel", help="Cancel a local job", parents=[_config_parent]
    )
    p_cancel.add_argument("job_id", type=int)
    p_cancel.set_defaults(func=cmd_job_cancel)

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
    p_del.add_argument("--full", action="store_true")
    p_del.add_argument("--prune", action="store_true")
    p_del.add_argument("--ttl-days", type=float, default=None)
    p_del.set_defaults(func=cmd_deletions)

    p_role_audit = sub.add_parser(
        "role-audit",
        help="Report principals whose roles break separation of duties",
        parents=[_config_parent],
    )
    p_role_audit.add_argument(
        "--strict", action="store_true", help="Exit non-zero when conflicts exist"
    )
    p_role_audit.set_defaults(func=cmd_role_audit)

    p_role_split = sub.add_parser(
        "role-split",
        help="Move a principal's extra roles onto new principals",
        parents=[_config_parent],
    )
    p_role_split.add_argument("--principal", type=int, required=True)
    p_role_split.add_argument("--keep", required=True)
    p_role_split.add_argument("--name", default=None)
    p_role_split.add_argument("--dry-run", action="store_true")
    p_role_split.set_defaults(func=cmd_role_split)

    return parser
