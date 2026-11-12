"""Stable identifiers for code entities and call instances."""

from __future__ import annotations

import re

_ARG_CANON_LIMIT = 64

_WS_RE = re.compile(r"\s+")
_STR_RE = re.compile(r"""^(['\"]).*?\1$""")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _canonical_arg(text: str) -> str:
    """Canonical shape of one argument for identity; display keeps the original."""
    raw = _WS_RE.sub(" ", (text or "").strip())
    if not raw:
        return "_"
    if "=" in raw and not raw.startswith(("=", "==")):
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            shaped = "_"
        elif not val:
            shaped = f"{key}=_"
        elif _STR_RE.match(val):
            shaped = f"{key}='...'"
        elif _NUM_RE.match(val):
            shaped = f"{key}=0"
        elif val in ("True", "False", "None"):
            shaped = f"{key}={val}"
        else:
            shaped = f"{key}=_"
    elif _STR_RE.match(raw):
        shaped = "'...'"
    elif _NUM_RE.match(raw):
        shaped = "0"
    elif raw in ("True", "False", "None"):
        shaped = raw
    else:
        shaped = "_"
    if len(shaped) > _ARG_CANON_LIMIT:
        shaped = shaped[:_ARG_CANON_LIMIT]
    return shaped


def entity_uid(version_id: int, qualified_name: str, kind: str) -> str:
    import hashlib

    blob = f"{version_id}\0{qualified_name}\0{kind}".encode()
    return hashlib.sha1(blob).hexdigest()


def call_uid(
    repo: str,
    caller_file: str,
    caller: str | None,
    start_byte: int,
    end_byte: int,
    callee: str,
    args: list[str],
) -> str:
    import hashlib

    shaped = [_canonical_arg(a) for a in (args or [])]
    blob = "\0".join(
        [
            repo,
            caller_file,
            caller or "",
            str(start_byte),
            str(end_byte),
            callee,
            "\x1f".join(shaped),
        ]
    ).encode()
    return hashlib.sha1(blob).hexdigest()
