"""Declarative command surface for the HTTP-only client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def parse_json(text: str) -> Any:
    return json.loads(text)


@dataclass(frozen=True)
class Param:
    name: str
    flags: tuple[str, ...]
    location: str
    action: str | None = None
    const: Any = None
    default: Any = None
    choices: tuple[str, ...] | None = None
    value_type: str = "str"
    required: bool = False
    help: str = ""

    @property
    def dest(self) -> str:
        return self.name.replace("-", "_")


def _param(
    location: str,
    name: str,
    *flags: str,
    action: str | None = None,
    const: Any = None,
    default: Any = None,
    choices: tuple[str, ...] | None = None,
    kind: str = "str",
    required: bool = False,
    help: str = "",
) -> Param:
    wire = flags or (
        (name,) if location == "path" else (f"--{name.replace('_', '-')}",)
    )
    return Param(
        name, wire, location, action, const, default, choices, kind, required, help
    )


def query(
    name: str,
    *flags: str,
    action: str | None = None,
    const: Any = None,
    default: Any = None,
    choices: tuple[str, ...] | None = None,
    kind: str = "str",
    required: bool = False,
    help: str = "",
) -> Param:
    return _param(
        "query",
        name,
        *flags,
        action=action,
        const=const,
        default=default,
        choices=choices,
        kind=kind,
        required=required,
        help=help,
    )


def body(
    name: str,
    *flags: str,
    action: str | None = None,
    const: Any = None,
    default: Any = None,
    choices: tuple[str, ...] | None = None,
    kind: str = "str",
    required: bool = False,
    help: str = "",
) -> Param:
    return _param(
        "body",
        name,
        *flags,
        action=action,
        const=const,
        default=default,
        choices=choices,
        kind=kind,
        required=required,
        help=help,
    )


def path(name: str, *flags: str, kind: str = "str", help: str = "") -> Param:
    return _param("path", name, *flags, kind=kind, required=True, help=help)


def local(
    name: str,
    *flags: str,
    action: str | None = None,
    default: Any = None,
    help: str = "",
) -> Param:
    return _param("local", name, *flags, action=action, default=default, help=help)


@dataclass(frozen=True)
class Command:
    name: str
    method: str
    path: str
    help: str
    params: tuple[Param, ...] = ()
    body_extra: dict[str, Any] = field(default_factory=dict)
    body_wrapper: str | None = None
    wait: str | None = None


_PRESET = ("precise", "balanced", "thorough")
_SCOPE = (
    query("library", "--library"),
    query("version", "--version"),
    query("ecosystem", "--ecosystem"),
)
_SOURCE_ROLES = (
    "official_example",
    "doc_snippet",
    "readme",
    "test",
    "reference_project",
    "implementation",
)

COMMANDS: tuple[Command, ...] = (
    Command("health", "GET", "/health", "Check server health"),
    Command("me", "GET", "/me", "Show the authenticated principal"),
    Command(
        "search",
        "GET",
        "/search",
        "Search indexed documentation",
        (
            query("query", "query", required=True, help="Non-empty query text"),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
            query("preset", "--preset", choices=_PRESET),
            query("suggest", "--suggest", action="store_true"),
            query("include_prerelease", "--include-prerelease", action="store_true"),
            query("block_type", "--block-type"),
            query("exclude_deprecated", "--exclude-deprecated", action="store_true"),
            query("aligned_only", "--aligned-only", action="store_true"),
        ),
    ),
    Command(
        "answer",
        "GET",
        "/answer",
        "Answer a question from indexed documentation",
        (
            query("query", "query", required=True),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
            query("preset", "--preset", choices=_PRESET),
        ),
    ),
    Command(
        "read",
        "GET",
        "/read",
        "Read a documentation page or chunk window",
        (
            query("url", "url", required=True),
            query("mode", "--mode", choices=("page", "window", "section")),
            query("sort_order", "--sort-order", kind="int"),
            query("radius", "--radius", kind="int"),
            query("source_group_id", "--source-group-id"),
            *_SCOPE,
        ),
    ),
    Command(
        "catalog",
        "GET",
        "/catalog",
        "List libraries or versions",
        (
            query("library", "library", required=False),
            query("ecosystem", "--ecosystem"),
        ),
    ),
    Command(
        "resolve",
        "GET",
        "/resolve",
        "Resolve a package to docs or repo metadata",
        (
            query("name", "name", required=True),
            query("ecosystem", "--ecosystem"),
            query("version", "--version"),
            query("target", "--target", choices=("docs", "repo")),
            query("include_prerelease", "--include-prerelease", action="store_true"),
        ),
    ),
    Command(
        "discover",
        "GET",
        "/library-search",
        "Discover packages across registries",
        (
            query("query", "query", required=True),
            query("ecosystem", "--ecosystem"),
            query("limit", "--limit", kind="int"),
            query("include_prerelease", "--include-prerelease", action="store_true"),
        ),
    ),
    Command(
        "search-web",
        "GET",
        "/web-search",
        "Unverified web search",
        (query("query", "query", required=True), query("limit", "--limit", kind="int")),
    ),
    Command(
        "search-github",
        "GET",
        "/github-search",
        "Unverified repository search",
        (query("query", "query", required=True), query("limit", "--limit", kind="int")),
    ),
    Command(
        "api-insight",
        "GET",
        "/api-insight",
        "Inspect API usage evidence",
        (
            query(
                "view",
                "view",
                choices=("usage", "related", "args", "common"),
                required=True,
            ),
            query("api", "--api"),
            query("apis", "--apis", action="append"),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
        ),
    ),
    Command(
        "pattern-insight",
        "GET",
        "/pattern-insight",
        "Inspect mined usage patterns",
        (
            query(
                "view",
                "view",
                choices=(
                    "patterns",
                    "evidence",
                    "tree",
                    "clusters",
                    "health",
                    "labels",
                    "snippets",
                    "facets",
                    "feature_summary",
                    "cluster_labels",
                    "drift",
                ),
                required=True,
            ),
            query("pattern", "--pattern", action="append"),
            query("containing", "--containing", action="append"),
            query("prefix", "--prefix"),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
            query("per_file_limit", "--per-file-limit", kind="int"),
            query("dedup", "--dedup", action="store_const", const=True),
            query("dedup", "--no-dedup", action="store_const", const=False),
            query("profile", "--profile"),
            query("facet", "--facet"),
            query("compare_profile", "--compare-profile"),
        ),
    ),
    Command(
        "doc-insight",
        "GET",
        "/doc-insight",
        "Inspect document-derived features",
        (
            query(
                "view",
                "view",
                choices=("features", "links", "coverage", "consistency"),
                required=True,
            ),
            query("library", "library", required=True),
            query("version", "version", required=True),
            query("ecosystem", "--ecosystem"),
            query("profile", "--profile"),
            query("facet", "--facet"),
            query("api", "--api"),
            query("pattern", "--pattern"),
            query("verdict", "--verdict"),
            query("block_type", "--block-type"),
            query("limit", "--limit", kind="int"),
        ),
    ),
    Command(
        "symbol-insight",
        "GET",
        "/symbol-insight",
        "Inspect extracted symbols",
        (
            query(
                "view",
                "view",
                choices=("detail", "file", "children", "signature"),
                required=True,
            ),
            query("api", "--api"),
            query("apis", "--apis", action="append"),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
            query("offset", "--offset", kind="int"),
        ),
    ),
    Command(
        "call-insight",
        "GET",
        "/call-insight",
        "Inspect call relations",
        (
            query("view", "view", choices=("callee", "caller", "file"), required=True),
            query("api", "--api"),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
            query("offset", "--offset", kind="int"),
            query("include_ambiguous", "--include-ambiguous", action="store_true"),
            query("resolve_kinds", "--resolve-kind", action="append"),
            query("relation_kinds", "--relation-kind", action="append"),
            query("min_args", "--min-args", kind="int"),
            query("max_args", "--max-args", kind="int"),
        ),
    ),
    Command(
        "import-insight",
        "GET",
        "/import-insight",
        "Inspect import relations",
        (
            query("view", "view", choices=("importers", "imports"), required=True),
            query("api", "--api"),
            *_SCOPE,
            query("limit", "--limit", kind="int"),
            query("offset", "--offset", kind="int"),
        ),
    ),
    Command(
        "link-stats",
        "GET",
        "/link-stats",
        "Inspect stored document links",
        (
            query("library", "library", required=True),
            query("version", "version", required=True),
            query("ecosystem", "--ecosystem"),
            query("limit", "--limit", kind="int"),
            query("url", "--url"),
        ),
    ),
    Command(
        "repo-deps",
        "GET",
        "/repo-deps",
        "List declared repository dependencies",
        (
            query("library", "library", required=True),
            query("version", "version", required=True),
            query("ecosystem", "--ecosystem"),
            query("dep_ecosystem", "--dep-ecosystem"),
        ),
    ),
    Command(
        "mining-health",
        "GET",
        "/mining-health",
        "Report mining coverage signals",
        (
            query("library", "--library"),
            query("version", "--version"),
            query("ecosystem", "--ecosystem"),
        ),
    ),
    Command(
        "doc-probe",
        "POST",
        "/doc-probe",
        "Probe a documentation site without storing",
        (
            body("start_url", "start_url", required=True),
            body("scope", "--scope", kind="json", help="JSON scope object"),
            body("sample_limit", "--sample-limit", kind="int"),
            body("fetch_sample_limit", "--fetch-sample-limit", kind="int"),
            body("max_pages", "--max-pages", kind="int"),
            body("prefilter", "--prefilter", action="store_true"),
        ),
    ),
    Command(
        "repo-probe",
        "POST",
        "/repo-probe",
        "Probe a remote repository layout",
        (
            body("source", "source", required=True),
            body("subdir", "--subdir"),
            body("max_files", "--max-files", kind="int"),
            body("sample_limit", "--sample-limit", kind="int"),
            body("only_files", "--only-file", action="append"),
            body("exclude_patterns", "--exclude-pattern", action="append"),
            body("tree_depth", "--tree-depth", kind="int"),
            body("samples_per_dir", "--samples-per-dir", kind="int"),
        ),
    ),
    Command(
        "ingest-docs",
        "POST",
        "/scrape",
        "Submit a documentation scrape job",
        (
            body("library", "library", required=True),
            body("version", "version", required=True),
            body("start_url", "start_url", required=True),
            body("ecosystem", "--ecosystem"),
            body("max_pages", "--max-pages", kind="int"),
        ),
    ),
    Command(
        "mine",
        "POST",
        "/jobs",
        "Submit a pattern mining job",
        (
            body("library", "library", required=True),
            body("version", "version", required=True),
            body("ecosystem", "--ecosystem"),
            body("minsup", "--minsup", kind="int"),
            body("topn", "--topn", kind="int"),
            body("maxlen", "--maxlen", kind="int"),
            body("minlen", "--minlen", kind="int"),
            body("include_ambiguous", "--include-ambiguous", action="store_true"),
            body("cluster_threshold", "--cluster-threshold", kind="float"),
            body("cluster_min_size", "--cluster-min-size", kind="int"),
            body("resolve_kinds", "--resolve-kind", action="append"),
            body("min_args", "--min-args", kind="int"),
            body("max_args", "--max-args", kind="int"),
            body("profile", "--profile"),
        ),
        body_extra={"kind": "mine_patterns"},
        body_wrapper="params",
    ),
    Command(
        "induce-labels",
        "POST",
        "/jobs",
        "Submit a label induction job",
        (
            body("library", "library", required=True),
            body("version", "version", required=True),
            body("ecosystem", "--ecosystem"),
            body("topn", "--topn", kind="int"),
            body("include_ambiguous", "--include-ambiguous", action="store_true"),
            body("profile", "--profile"),
        ),
        body_extra={"kind": "induce_labels"},
        body_wrapper="params",
    ),
    Command(
        "request submit",
        "POST",
        "/index-requests",
        "Submit an index request",
        (
            body("kind", "--kind", choices=("repo", "docs"), required=True),
            body("library", "--library", required=True),
            body("version", "--version"),
            body("ecosystem", "--ecosystem"),
            body("source", "--source"),
            body("start_url", "--start-url"),
            body("max_pages", "--max-pages", kind="int"),
            body("scope", "--scope", kind="json"),
            body("subdir", "--subdir"),
            body("only_files", "--only-file", action="append"),
            body("exclude_patterns", "--exclude-pattern", action="append"),
            body("max_files", "--max-files", kind="int"),
            body("source_role", "--source-role", choices=_SOURCE_ROLES),
            body("target_filter", "--target-filter", kind="json"),
            local("target_prefix", "--target-prefix", action="append"),
            local("target_name", "--target-name", action="append"),
            body("prefilter", "--prefilter", action="store_true"),
            body("update", "--update", action="store_true"),
            body("fresh", "--fresh", action="store_true"),
            body("include_prerelease", "--include-prerelease", action="store_true"),
        ),
    ),
    Command(
        "request list",
        "GET",
        "/index-requests",
        "List your index requests",
        (
            query("status", "--status"),
            local("pending", "--pending", action="store_true"),
            query("limit", "--limit", kind="int"),
        ),
    ),
    Command(
        "request show",
        "GET",
        "/index-requests/{request_id}",
        "Show one index request",
        (path("request_id", "request_id", kind="int"),),
    ),
    Command(
        "request approve",
        "POST",
        "/index-requests/{request_id}/approve",
        "Approve a pending request",
        (path("request_id", "request_id", kind="int"),),
    ),
    Command(
        "request reject",
        "POST",
        "/index-requests/{request_id}/reject",
        "Reject a pending request",
        (
            path("request_id", "request_id", kind="int"),
            body("reason", "--reason", required=True),
        ),
    ),
    Command(
        "request cancel",
        "POST",
        "/index-requests/{request_id}/cancel",
        "Cancel your pending request",
        (path("request_id", "request_id", kind="int"),),
    ),
    Command(
        "request wait",
        "GET",
        "/index-requests/{request_id}",
        "Wait for a terminal index request state",
        (path("request_id", "request_id", kind="int"),),
        wait="request",
    ),
    Command(
        "job list",
        "GET",
        "/jobs",
        "List recent jobs",
        (query("limit", "--limit", kind="int"),),
    ),
    Command(
        "job show",
        "GET",
        "/jobs/{job_id}",
        "Show one job",
        (path("job_id", "job_id", kind="int"),),
    ),
    Command(
        "job cancel",
        "POST",
        "/jobs/{job_id}/cancel",
        "Cancel a job",
        (path("job_id", "job_id", kind="int"),),
    ),
    Command(
        "job wait",
        "GET",
        "/jobs/{job_id}",
        "Wait for a terminal job state",
        (path("job_id", "job_id", kind="int"),),
        wait="job",
    ),
    Command(
        "review list",
        "GET",
        "/review",
        "List pending review items",
        (query("limit", "--limit", kind="int"),),
    ),
    Command(
        "review approve",
        "POST",
        "/review",
        "Approve a review item",
        (body("entity_id", "entity_id", kind="int", required=True),),
        body_extra={"action": "approve"},
    ),
    Command(
        "review conflict",
        "POST",
        "/review",
        "Mark a review item as conflicted",
        (body("entity_id", "entity_id", kind="int", required=True),),
        body_extra={"action": "conflict"},
    ),
    Command(
        "review reopen",
        "POST",
        "/review",
        "Reopen a review item",
        (body("entity_id", "entity_id", kind="int", required=True),),
        body_extra={"action": "reopen"},
    ),
    Command(
        "feedback submit",
        "POST",
        "/feedback",
        "Submit feedback",
        (
            body("entity_type", "--entity-type"),
            body("entity_id", "--entity-id", kind="int"),
            body("kind", "--kind"),
            body("body", "--body", required=True),
        ),
    ),
    Command(
        "feedback list",
        "GET",
        "/feedback",
        "List feedback items",
        (
            query("limit", "--limit", kind="int"),
            query("status", "--status", choices=("open", "acknowledged", "resolved")),
        ),
    ),
    Command(
        "feedback resolve",
        "POST",
        "/feedback/{feedback_id}/resolve",
        "Resolve feedback",
        (path("feedback_id", "feedback_id", kind="int"),),
    ),
    Command(
        "admin principal list",
        "GET",
        "/admin/principals",
        "List principals (requires admin:keys)",
        (query("tenant_id", "--tenant-id"),),
    ),
    Command(
        "admin key list",
        "GET",
        "/admin/keys",
        "List key metadata (requires admin:keys)",
        (query("tenant_id", "--tenant-id"),),
    ),
    Command(
        "admin key issue",
        "POST",
        "/admin/keys",
        "Create a principal and issue a key (requires admin:keys)",
        (
            body("name", "--name", required=True),
            body("kind", "--kind"),
            body("tenant_id", "--tenant-id"),
            body("roles", "--role", action="append"),
            body("scopes", "--scope", action="append"),
            body("expires_at", "--expires-at"),
        ),
    ),
    Command(
        "admin key attach",
        "POST",
        "/admin/principals/{principal_id}/keys",
        "Issue another key for a principal (requires admin:keys)",
        (
            path("principal_id", "principal_id", kind="int"),
            body("scopes", "--scope", action="append"),
            body("expires_at", "--expires-at"),
        ),
    ),
    Command(
        "admin key revoke",
        "DELETE",
        "/admin/keys/{key_id}",
        "Revoke one key (requires admin:keys)",
        (path("key_id", "key_id", kind="int"),),
    ),
    Command(
        "admin key revoke-all",
        "DELETE",
        "/admin/principals/{principal_id}/keys",
        "Revoke all keys of a principal (requires admin:keys)",
        (path("principal_id", "principal_id", kind="int"),),
    ),
)

BY_NAME = {command.name: command for command in COMMANDS}
