"""Argument parser for the server-local operations CLI."""

from __future__ import annotations

import argparse

from libref.cli.common import _config_parent
from libref.cli.key_commands import (
    cmd_key_attach,
    cmd_key_issue,
    cmd_key_list,
    cmd_key_revoke,
    cmd_key_revoke_all,
    cmd_principal_list,
)
from libref.cli.local_commands import (
    cmd_doc_features,
    cmd_index_repo,
    cmd_induce_labels,
    cmd_mine,
    cmd_probe_repo,
)
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

    p_local = sub.add_parser(
        "local",
        help="Direct host-path indexing, probing and mining",
        parents=[_config_parent],
    )
    _add_local_commands(p_local.add_subparsers(dest="local_command", required=True))
    _add_key_commands(sub)

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


def _add_local_commands(sub: argparse._SubParsersAction) -> None:
    source_roles = (
        "official_example",
        "doc_snippet",
        "readme",
        "test",
        "reference_project",
        "implementation",
    )

    p_index = sub.add_parser(
        "index-repo",
        help="Index a repository from a host path",
        parents=[_config_parent],
    )
    p_index.add_argument("library")
    p_index.add_argument("version")
    p_index.add_argument(
        "source", help="Host path; remote sources use librefctl request submit"
    )
    p_index.add_argument("--ecosystem", default=None)
    p_index.add_argument("--update", action="store_true")
    p_index.add_argument("--fresh", action="store_true")
    p_index.add_argument("--subdir", default=None)
    p_index.add_argument("--only-file", action="append", default=None)
    p_index.add_argument("--exclude-pattern", action="append", default=None)
    p_index.add_argument("--max-files", type=int, default=None)
    p_index.add_argument(
        "--source-role", choices=source_roles, default="implementation"
    )
    p_index.add_argument("--target-prefix", action="append", default=None)
    p_index.add_argument("--target-name", action="append", default=None)
    p_index.set_defaults(func=cmd_index_repo)

    p_probe_repo = sub.add_parser(
        "probe-repo",
        help="Inspect a local or remote repository without storing it",
        parents=[_config_parent],
    )
    p_probe_repo.add_argument("source", help="Host path or explicit HTTP(S) address")
    p_probe_repo.add_argument("--subdir", default=None)
    p_probe_repo.add_argument("--only-file", action="append", default=None)
    p_probe_repo.add_argument("--exclude-pattern", action="append", default=None)
    p_probe_repo.add_argument("--max-files", type=int, default=None)
    p_probe_repo.add_argument("--sample-limit", type=int, default=None)
    p_probe_repo.add_argument("--tree-depth", type=int, default=None)
    p_probe_repo.add_argument("--samples-per-dir", type=int, default=None)
    p_probe_repo.set_defaults(func=cmd_probe_repo)

    p_mine = sub.add_parser(
        "mine",
        help="Mine call-sequence patterns directly from stored evidence",
        parents=[_config_parent],
    )
    p_mine.add_argument("library")
    p_mine.add_argument("version")
    p_mine.add_argument("--ecosystem", default=None)
    p_mine.add_argument("--minsup", type=int, default=None)
    p_mine.add_argument("--topn", type=int, default=None)
    p_mine.add_argument("--maxlen", type=int, default=None)
    p_mine.add_argument("--minlen", type=int, default=None)
    p_mine.add_argument("--include-ambiguous", action="store_true")
    p_mine.add_argument("--cluster-threshold", type=float, default=None)
    p_mine.add_argument("--cluster-min-size", type=int, default=None)
    p_mine.add_argument("--resolve-kind", action="append", default=None)
    p_mine.add_argument("--min-args", type=int, default=None)
    p_mine.add_argument("--max-args", type=int, default=None)
    p_mine.add_argument("--profile", default=None)
    p_mine.add_argument("--source-role", action="append", default=None)
    p_mine.add_argument(
        "--macro-policy",
        choices=("exclude", "review_only", "approved_only"),
        default=None,
    )
    p_mine.set_defaults(func=cmd_mine)

    p_induce = sub.add_parser(
        "induce-labels",
        help="Induce pattern labels directly with the local chat endpoint",
        parents=[_config_parent],
    )
    p_induce.add_argument("library")
    p_induce.add_argument("version")
    p_induce.add_argument("--ecosystem", default=None)
    p_induce.add_argument("--topn", type=int, default=None)
    p_induce.add_argument("--include-ambiguous", action="store_true")
    p_induce.add_argument("--profile", default=None)
    p_induce.add_argument(
        "--macro-policy",
        choices=("exclude", "review_only", "approved_only"),
        default=None,
    )
    p_induce.set_defaults(func=cmd_induce_labels)

    p_features = sub.add_parser(
        "doc-features",
        help="Rebuild document-derived features for a stored version",
        parents=[_config_parent],
    )
    feature_sub = p_features.add_subparsers(dest="doc_feature_command", required=True)
    p_refresh = feature_sub.add_parser("refresh", help="Rebuild one derivation scope")
    p_refresh.add_argument("--library", required=True)
    p_refresh.add_argument("--version", required=True)
    p_refresh.add_argument("--ecosystem", default=None)
    p_refresh.add_argument("--profile", default=None)
    p_refresh.add_argument(
        "--scope",
        choices=(
            "structure",
            "links",
            "sources",
            "coverage",
            "consistency",
            "derived",
            "all",
        ),
        default="all",
    )
    p_features.set_defaults(func=cmd_doc_features)


def _add_key_commands(sub: argparse._SubParsersAction) -> None:
    from libref.services.infra.auth import KNOWN_SCOPES, ROLE_SCOPES

    p_principal = sub.add_parser(
        "principal",
        help="List local principals",
        parents=[_config_parent],
    )
    principal_sub = p_principal.add_subparsers(dest="principal_action", required=True)
    p_principal_list = principal_sub.add_parser("list", help="List principals")
    p_principal_list.add_argument("--tenant", default=None)
    p_principal.set_defaults(func=cmd_principal_list)

    p_key = sub.add_parser(
        "key",
        help="Manage local API keys",
        parents=[_config_parent],
    )
    key_sub = p_key.add_subparsers(dest="key_action", required=True)

    p_list = key_sub.add_parser("list", help="List key metadata")
    p_list.add_argument("--tenant", default=None)
    p_list.set_defaults(func=cmd_key_list)

    p_issue = key_sub.add_parser("issue", help="Create a principal and issue a key")
    p_issue.add_argument("--name", required=True)
    p_issue.add_argument("--kind", default="human")
    p_issue.add_argument("--tenant", default="default")
    p_issue.add_argument("--role", action="append", choices=sorted(ROLE_SCOPES))
    p_issue.add_argument("--scope", action="append", choices=sorted(KNOWN_SCOPES))
    p_issue.add_argument("--expires-at", default=None, help="ISO 8601 instant")
    p_issue.set_defaults(func=cmd_key_issue)

    p_attach = key_sub.add_parser("attach", help="Issue another key for a principal")
    p_attach.add_argument("principal_id", type=int)
    p_attach.add_argument("--scope", action="append", choices=sorted(KNOWN_SCOPES))
    p_attach.add_argument("--expires-at", default=None, help="ISO 8601 instant")
    p_attach.set_defaults(func=cmd_key_attach)

    p_revoke = key_sub.add_parser("revoke", help="Revoke one key")
    p_revoke.add_argument("key_id", type=int)
    p_revoke.set_defaults(func=cmd_key_revoke)

    p_revoke_all = key_sub.add_parser(
        "revoke-all", help="Revoke all keys of a principal"
    )
    p_revoke_all.add_argument("principal_id", type=int)
    p_revoke_all.set_defaults(func=cmd_key_revoke_all)
