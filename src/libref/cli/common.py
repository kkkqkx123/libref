"""Shared CLI wiring: settings, connections, scope and print helpers."""

from __future__ import annotations

import argparse
import sys

from libref.config import LibrefSettings, load_settings
from libref.db import apply_migrations, open_db
from libref.errors import LibrefError, exit_code_for, normalize
from libref.observe import setup_logging
from libref.services.factory import build_discovery_service, build_vector

_config_parent = argparse.ArgumentParser(add_help=False)
_config_parent.add_argument(
    "--config",
    default=None,
    help="Config file path; by default looked up in the current directory, then the user config directory",
)
_config_parent.add_argument(
    "--log-level",
    default=None,
    help="Log level: DEBUG/INFO/WARNING/ERROR; defaults to the config value",
)
_config_parent.add_argument(
    "--log-file",
    default=None,
    help="Log file path; defaults to the config value; when unset, only stderr is used",
)
_config_parent.add_argument(
    "--log-format",
    default=None,
    help="Log format: text/json; defaults to the config value",
)
_config_parent.add_argument(
    "--remote",
    action="store_true",
    help="Send the request to a running server instead of opening the local database",
)
_config_parent.add_argument(
    "--base-url",
    default=None,
    help="Server base URL for --remote; defaults to the client.base_url setting",
)
_config_parent.add_argument(
    "--json",
    action="store_true",
    help="Print failures as one JSON object on stdout; results keep their own format",
)


def _load_settings(args: argparse.Namespace, overrides: dict | None = None):
    merged = dict(overrides or {})
    if getattr(args, "log_level", None) is not None:
        merged["log_level"] = args.log_level
    if getattr(args, "log_file", None) is not None:
        merged["log_file"] = args.log_file
    if getattr(args, "log_format", None) is not None:
        merged["log_format"] = args.log_format
    settings = load_settings(getattr(args, "config", None), merged)
    setup_logging(settings)
    return settings


def _split_tool_arg(text: str) -> list[str]:
    """Split a comma-separated tool list from the CLI; blanks drop out."""
    return [part.strip().lower() for part in text.split(",") if part.strip()]


def _mcp_tools_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    if getattr(args, "enabled_tools", None) is not None:
        overrides["mcp_enabled_tools"] = _split_tool_arg(args.enabled_tools)
    if getattr(args, "disabled_tools", None) is not None:
        overrides["mcp_disabled_tools"] = _split_tool_arg(args.disabled_tools)
    return overrides


def _registry_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    if getattr(args, "offline", False):
        overrides["registry_offline"] = True
    if getattr(args, "registry_mirror", None) is not None:
        overrides["registry_mirror"] = args.registry_mirror
    return overrides


def _window_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}
    if getattr(args, "expand_window", False):
        overrides["retriever_expand_window"] = True
    if getattr(args, "window_radius", None) is not None:
        overrides["retriever_window_radius"] = int(args.window_radius)
    if getattr(args, "material_limit", None) is not None:
        overrides["material_limit"] = int(args.material_limit)
    return overrides


def _connect(settings: LibrefSettings, *, check_same_thread: bool = True):
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(settings.db_path, check_same_thread=check_same_thread)
    apply_migrations(conn)
    return conn


def applied_count(conn) -> int:
    from libref.db.migrations import MIGRATIONS, applied_versions

    return len(applied_versions(conn) & {m.version for m in MIGRATIONS})


def _build_vector(settings: LibrefSettings, gateway=None):
    """Assemble vector store and embed function via the composition root."""
    vec, embed, _gw = build_vector(settings, gateway)
    return vec, embed


def _scope_from_args(args: argparse.Namespace) -> dict:
    return {
        "allow_cross_site": bool(getattr(args, "allow_cross_site", False)),
        "allow_subdomains": bool(getattr(args, "allow_subdomains", False)),
        "path_prefixes": list(getattr(args, "path_prefix", None) or []),
        "path_suffixes": list(getattr(args, "path_suffix", None) or []),
        "include_patterns": list(getattr(args, "include_pattern", None) or []),
        "exclude_patterns": list(getattr(args, "exclude_pattern", None) or []),
        "urls": list(getattr(args, "url", None) or []),
        "max_depth": getattr(args, "max_depth", None),
        "render_js": bool(getattr(args, "render_js", False)),
        "render_wait_for": getattr(args, "render_wait_for", None),
        "render_timeout_ms": getattr(args, "render_timeout_ms", None),
        "follow_links": bool(getattr(args, "follow_links", False)),
        "crawl_query": getattr(args, "crawl_query", None),
        "score_threshold": getattr(args, "score_threshold", None),
        "bypass_cache": bool(getattr(args, "bypass_cache", False)),
    }


def _resolve_for_cli(
    settings, conn, ecosystem, library, requested_version, include_prerelease=False
):
    """CLI resolution form: ecosystem plus name resolve to address and version; on failure print the classified error."""
    import asyncio

    from libref.crawler.http import build_async_client
    from libref.services.registry.resolver import resolve

    async def _run():
        async with build_async_client(settings) as client:
            return await resolve(
                conn,
                client,
                settings,
                ecosystem,
                library,
                requested_version,
                "docs",
                include_prerelease,
            )

    return asyncio.run(_run())


def _print_suggestions(conn, settings, args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import json as _json

    try:
        candidates = _asyncio.run(
            build_discovery_service(conn, settings).search_async(
                getattr(args, "library", None) or args.query,
                getattr(args, "ecosystem", None),
                args.limit,
                include_prerelease=bool(getattr(args, "include_prerelease", False)),
            )
        )
    except Exception as exc:
        raise as_error(exc) from exc
    if not candidates:
        print("(no suggestions)")
        return 0
    print("suggestions:")
    for c in candidates:
        print(_json.dumps(c, ensure_ascii=False))
    return 0


def _print_job(job) -> None:
    print(f"job {job.id} {job.status}" + (f" error={job.error}" if job.error else ""))
    result = getattr(job, "result", None)
    if result:
        import json as _json

        print(f"result: {_json.dumps(result, ensure_ascii=False)[:2000]}")
    if getattr(job, "finished_at", None):
        print(f"finished_at: {job.finished_at}")


def as_error(exc: BaseException) -> LibrefError:
    """Classify a failure for the exit boundary; unknown ones keep their traceback."""
    found = normalize(exc)
    if found is None:
        raise exc
    return found


def render_error(error: LibrefError) -> str:
    """One code line, then the follow-ups a caller can take."""
    lines = [f"{error.code}: {error.message}"]
    if error.hint:
        lines.append(f"  hint: {error.hint}")
    for action in error.next_actions or []:
        if action.get("kind") == "tool" and action.get("cli"):
            lines.append(f"  try: {action['cli']} : {action.get('purpose', '')}")
        elif action.get("detail"):
            lines.append(f"  try: {action['detail']}")
    if error.request_id:
        lines.append(f"  request_id: {error.request_id}")
    return "\n".join(lines)


def report_failure(error: LibrefError, args: argparse.Namespace | None) -> int:
    """Print a failure and return the exit status its code implies."""
    print(render_error(error), file=sys.stderr)
    if getattr(args, "json", False):
        print(error.to_json())
    return exit_code_for(error.code)
