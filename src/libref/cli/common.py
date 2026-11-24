"""Shared wiring for the server-local operations CLI."""

from __future__ import annotations

import argparse
import sys

from libref.config import LibrefSettings, load_settings
from libref.db import apply_migrations, open_db
from libref.errors import LibrefError, exit_code_for, normalize
from libref.observe import setup_logging
from libref.services.factory import build_vector

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
    "--json",
    action="store_true",
    help="Print failures as one JSON object on stdout",
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
