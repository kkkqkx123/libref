"""Argument contract for MCP tool calls.

The SDK validates arguments with a model built from the tool signature, which
drops unknown keys and coerces a boolean into an integer. A caller that mistypes
a parameter therefore gets an ordinary result and no signal that anything was
ignored. This middleware validates the raw arguments against the tool's
published schema before the call reaches the tool, so the caller learns the
offending field and, for an unknown key, the accepted ones.

The check is deliberately the only place that reads the raw request: the SDK's
own validation stays as a backstop for anything this module misses.
"""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from libref.errors import LibrefError, invalid_argument

_TYPE_WORDS = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "array": "an array",
    "object": "an object",
    "null": "null",
}

_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

_CRASH_PREFIX = "Error executing tool"


class ToolFailure(LibrefError, ToolError):
    """A ``LibrefError`` the MCP runner reports as an anticipated failure.

    The runner keeps the message of a ``ToolError`` and replaces anything else
    with ``Error executing tool <name>``, so every error a tool raises has to be
    one. The compact payload reaches the caller unchanged.
    """


def failed(error: LibrefError) -> ToolFailure:
    """Re-raise a shared error value in the shape the runner preserves."""
    if isinstance(error, ToolFailure):
        return error
    return ToolFailure(
        error.code,
        error.message,
        field=error.field,
        hint=error.hint,
        next_actions=error.next_actions,
        request_id=error.request_id,
    )


def rejected(error: LibrefError) -> CallToolResult:
    """Render a contract failure as the one result shape a caller must handle."""
    return CallToolResult(
        content=[TextContent(type="text", text=error.to_json())], is_error=True
    )


class ArgumentContract:
    """``ServerMiddleware`` validating ``tools/call`` arguments against the schema."""

    def __init__(self, mcp: MCPServer) -> None:
        self._mcp = mcp
        self._schemas: dict[str, dict[str, Any]] = {}
        self._loaded = False

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: Any
    ) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, dict) else {}
        name = params.get("name")
        if not isinstance(name, str):
            return await call_next(ctx)
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return rejected(
                invalid_argument("arguments must be an object", field="arguments")
            )
        problem = await self._check(name, arguments)
        if problem is not None:
            return rejected(problem)
        return _normalize_result(await call_next(ctx))

    async def _check(self, name: str, arguments: dict[str, Any]) -> LibrefError | None:
        schema = await self._schema(name)
        if schema is None:
            return None
        properties: dict[str, Any] = schema.get("properties") or {}
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            names = sorted(properties)
            priority: list[str] = [
                n for n in ("query", "limit", "preset") if n in names
            ]
            rest = [n for n in names if n not in priority]
            chosen: list[str] = list(priority)
            for candidate_name in rest:
                candidate = ", ".join([*chosen, candidate_name])
                if len(candidate) > 72:
                    break
                chosen.append(candidate_name)
            remaining = len(names) - len(chosen)
            accepted = ", ".join(chosen) + (f", +{remaining} more" if remaining else "")
            return invalid_argument(
                f"unknown parameter: {unknown[0]}",
                field=unknown[0],
                hint=f"accepted: {accepted}",
            )
        validator = Draft202012Validator(schema)
        failure = best_match(validator.iter_errors(_coerce(arguments, properties)))
        if failure is None:
            return None
        return _describe(failure)

    async def _schema(self, name: str) -> dict[str, Any] | None:
        if not self._loaded:
            for tool in await self._mcp.list_tools():
                self._schemas[tool.name] = tool.input_schema
            self._loaded = True
        return self._schemas.get(name)


def install(mcp: MCPServer) -> None:
    """Register the contract on a server whose tools are already registered."""
    mcp.middleware.append(ArgumentContract(mcp))


def _normalize_result(result: Any) -> Any:
    """Reduce a tool failure to the compact payload alone.

    The runner prefixes every failure with ``Error executing tool <name>``. That
    prefix is noise for a caller that already knows which tool it called, and it
    would push a one-sentence error past the size budget. A failure whose text is
    only the prefix is a crash: it carries no payload, so the caller gets one
    retry signal while the server log keeps the traceback.

    A middleware sees the wire shape (a serialized dict), so both spellings are
    accepted.
    """
    if isinstance(result, CallToolResult):
        body: dict[str, Any] = result.model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
    elif isinstance(result, dict):
        body = result
    else:
        return result
    if not body.get("isError"):
        return _compact(body) or result
    content = body.get("content") or []
    first = content[0] if content and isinstance(content[0], dict) else {}
    text = str(first.get("text") or "")
    if not text.startswith(_CRASH_PREFIX):
        return result
    _, _, payload = text[len(_CRASH_PREFIX) :].partition(": ")
    if not payload.startswith("{"):
        return rejected(
            LibrefError(
                "unavailable",
                "the tool failed before producing a result",
                hint="retry once; if it repeats, the server log holds the cause",
            )
        )
    return {**body, "content": [{"type": "text", "text": payload}]}


def _branch(schema: dict[str, Any]) -> dict[str, Any]:
    """The non-null arm of an optional field, so type wording stays concrete."""
    arms = schema.get("anyOf") or schema.get("oneOf")
    if not arms:
        return schema
    for arm in arms:
        if arm.get("type") != "null":
            return arm
    return schema


def _coerce(arguments: dict[str, Any], properties: dict[str, Any]) -> dict[str, Any]:
    """Accept the string spellings the SDK accepts; never accept a boolean as an integer."""
    out = dict(arguments)
    for key, value in arguments.items():
        if not isinstance(value, str):
            continue
        schema = _branch(properties.get(key) or {})
        kind = schema.get("type")
        if kind == "integer" and _INTEGER.match(value):
            out[key] = int(value)
        elif kind == "number" and _NUMBER.match(value):
            out[key] = float(value)
        elif kind == "boolean" and value.lower() in ("true", "false"):
            out[key] = value.lower() == "true"
        elif kind in ("array", "object") and value[:1] in ("[", "{"):
            try:
                out[key] = json.loads(value)
            except ValueError:
                pass
    return out


def _describe(failure: Any) -> LibrefError:
    """One sentence naming the field and the expectation behind a schema violation."""
    field = ".".join(str(part) for part in failure.absolute_path) or None
    validator = failure.validator
    schema = failure.schema if isinstance(failure.schema, dict) else {}

    if validator == "required":
        absent = [key for key in failure.validator_value if key not in failure.instance]
        name = absent[0] if absent else field
        return invalid_argument(f"{name} is required", field=name)

    if validator in ("anyOf", "oneOf"):
        inner = _narrow(failure)
        if inner is not None:
            return _describe(inner)

    if validator == "type":
        return invalid_argument(f"{field} must be {_type_phrase(schema)}", field=field)

    if validator == "enum":
        options = ", ".join(str(option) for option in failure.validator_value)
        return invalid_argument(f"{field} must be one of: {options}", field=field)

    if validator == "const":
        return invalid_argument(
            f"{field} must be {failure.validator_value}", field=field
        )

    if validator in ("minimum", "maximum"):
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and high is not None:
            message = f"{field} must be between {low} and {high}"
        elif low is not None:
            message = f"{field} must be at least {low}"
        else:
            message = f"{field} must be at most {high}"
        return invalid_argument(message, field=field)

    fallback = f"{field} is invalid" if field else "arguments are invalid"
    return invalid_argument(fallback, field=field)


def _narrow(failure: Any) -> Any | None:
    """Pick the failing arm that describes the value, ignoring the null arm."""
    arms = [
        child
        for child in failure.context
        if not (isinstance(child.schema, dict) and child.schema.get("type") == "null")
    ]
    return best_match(arms) if arms else None


def _type_phrase(schema: dict[str, Any]) -> str:
    kind = str(schema.get("type") or "")
    if kind == "array":
        items = schema.get("items") or {}
        inner = items.get("type")
        return (
            f"an array of {_TYPE_WORDS.get(inner, 'values')}" if inner else "an array"
        )
    return _TYPE_WORDS.get(kind, "the expected type")


def _compact(body: dict[str, Any]) -> dict[str, Any] | None:
    """Render a success payload once instead of twice.

    The runner prints the structured value into ``content`` with two-space
    indentation while repeating it in ``structuredContent``. Indentation alone
    costs about a quarter of the payload, so the text form is re-emitted
    compactly and both members stay available.
    """
    structured = body.get("structuredContent")
    if not isinstance(structured, dict):
        return None
    text = json.dumps(structured, separators=(",", ":"), ensure_ascii=False)
    content = body.get("content")
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("text") == text
    ):
        return None
    return {**body, "content": [{"type": "text", "text": text}]}
