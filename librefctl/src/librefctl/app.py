"""Argument parsing, dispatch and process boundary for librefctl."""

from __future__ import annotations

import argparse
import os
import time
from typing import Any, NoReturn
from urllib.parse import quote

from librefctl.commands import COMMANDS, Command, Param, parse_json
from librefctl.output import emit, exit_code, failure, success
from librefctl.transport import Client, ClientError

_TERMINAL = {
    "request": frozenset({"done", "failed", "rejected", "cancelled"}),
    "job": frozenset({"done", "failed"}),
}


class _JsonArgumentParser(argparse.ArgumentParser):
    """Keep usage failures inside the same JSON error envelope."""

    def error(self, message: str) -> NoReturn:
        emit(failure(ClientError("invalid_argument", message)))
        raise SystemExit(2)


def _type(value_type: str):
    return {"int": int, "float": float, "json": parse_json}.get(value_type, str)


def _add_param(parser: argparse.ArgumentParser, param: Param) -> None:
    kwargs: dict[str, Any] = {
        "default": param.default,
        "help": param.help,
    }
    if param.flags[0].startswith("-"):
        kwargs["dest"] = param.dest
        kwargs["required"] = param.required
        if param.choices is not None:
            kwargs["choices"] = list(param.choices)
        if param.action == "store_const":
            kwargs["action"] = "store_const"
            kwargs["const"] = param.const
        elif param.action:
            kwargs["action"] = param.action
        else:
            kwargs["type"] = _type(param.value_type)
    else:
        kwargs["type"] = _type(param.value_type)
        if param.default is None and not param.required:
            kwargs["nargs"] = "?"
    parser.add_argument(*param.flags, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="librefctl",
        description="HTTP-only client for a running libref server",
    )
    try:
        timeout_default = float(os.environ.get("LIBREF_CLIENT_TIMEOUT", "30"))
    except ValueError:
        parser.error("LIBREF_CLIENT_TIMEOUT must be a number")
        raise AssertionError("unreachable")  # pragma: no cover
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LIBREF_BASE_URL", "http://127.0.0.1:8001"),
        help="Server base URL; defaults to LIBREF_BASE_URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("LIBREF_CLIENT_API_KEY", ""),
        help="Bearer key; defaults to LIBREF_CLIENT_API_KEY",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=timeout_default,
        help="Per-request timeout in seconds",
    )
    parser.add_argument("--pretty", action="store_true", help="Indent the JSON output")

    root = parser.add_subparsers(dest="_root", required=True)
    groups: dict[tuple[str, ...], Any] = {}
    for command in COMMANDS:
        parts = command.name.split()
        parent = root
        for depth in range(1, len(parts)):
            key = tuple(parts[:depth])
            if key not in groups:
                group = parent.add_parser(key[-1], help=f"{' '.join(key)} commands")
                groups[key] = group.add_subparsers(dest=f"_sub{depth}", required=True)
            parent = groups[key]
        leaf = parent.add_parser(parts[-1], help=command.help)
        for param in command.params:
            _add_param(leaf, param)
        if command.wait:
            leaf.add_argument(
                "--timeout",
                type=float,
                default=300.0,
                help="Maximum seconds to wait for a terminal state",
            )
        leaf.set_defaults(_command=command)
    return parser


def _collect(
    command: Command, args: argparse.Namespace
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    path_values: dict[str, Any] = {}
    query: dict[str, Any] = {}
    payload: dict[str, Any] = {}
    local: dict[str, Any] = {}
    for param in command.params:
        value = getattr(args, param.dest, None)
        if param.location == "local":
            local[param.name] = value
            continue
        if value is None:
            continue
        if param.location == "path":
            path_values[param.name] = value
        elif param.location == "query":
            query[param.name] = value
        else:
            payload[param.name] = value
    return path_values, query, payload, local


def _request_body(command: Command, payload: dict[str, Any], local: dict[str, Any]):
    target_filter = payload.get("target_filter")
    if target_filter is not None and not isinstance(target_filter, dict):
        raise ClientError(
            "invalid_argument",
            "target_filter must be a JSON object",
            field="target_filter",
        )
    if local.get("target_prefix") or local.get("target_name"):
        target = dict(target_filter or {})
        if local.get("target_prefix"):
            target["prefixes"] = local["target_prefix"]
        if local.get("target_name"):
            target["names"] = local["target_name"]
        payload["target_filter"] = target
    if command.body_wrapper:
        return {**command.body_extra, command.body_wrapper: payload}
    return {**command.body_extra, **payload}


def _wait(
    client: Client, command: Command, path: str, args: argparse.Namespace
) -> tuple[Any, dict[str, Any]]:
    assert command.wait is not None
    deadline = time.monotonic() + float(args.timeout)
    attempts = 0
    while True:
        data = client.call("GET", path)
        attempts += 1
        status = str(data.get("status") or "")
        if status in _TERMINAL[command.wait]:
            return data, {"waited": True, "status": status, "attempts": attempts}
        if time.monotonic() >= deadline:
            raise ClientError(
                "unavailable", f"timed out waiting for {command.wait} terminal state"
            )
        time.sleep(1.0)


def dispatch(client: Client, args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    command: Command = args._command
    path_values, query, payload, local = _collect(command, args)
    path = command.path.format(
        **{key: quote(str(value), safe="") for key, value in path_values.items()}
    )
    if local.get("pending"):
        path = "/index-requests/pending"
        query.pop("status", None)
    if command.method == "GET":
        data = client.call(command.method, path, params=query)
    else:
        data = client.call(
            command.method, path, payload=_request_body(command, payload, local)
        )
    if command.wait:
        return _wait(client, command, path, args)
    return data, {}


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    client = Client(args.base_url, args.api_key, args.request_timeout)
    try:
        data, meta = dispatch(client, args)
        emit(success(data, meta), args.pretty)
        return 0
    except ClientError as exc:
        emit(failure(exc), args.pretty)
        return exit_code(exc.code)
    finally:
        client.close()
