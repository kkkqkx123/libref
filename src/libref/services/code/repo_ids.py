"""Stable identifiers for code entities and call instances."""

from __future__ import annotations


def entity_uid(version_id: int, qualified_name: str, kind: str) -> str:
    import hashlib

    blob = f"{version_id}\0{qualified_name}\0{kind}".encode()
    return hashlib.sha1(blob).hexdigest()


def call_uid(
    repo: str,
    caller_file: str,
    line_start: int,
    line_end: int,
    callee: str,
    args: list[str],
) -> str:
    import hashlib

    blob = "\0".join(
        [repo, caller_file, str(line_start), str(line_end), callee, "\x1f".join(args)]
    ).encode()
    return hashlib.sha1(blob).hexdigest()
