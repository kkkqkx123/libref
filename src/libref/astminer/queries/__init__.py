"""Query registry: fetch pattern text by language and query type, with a unified fingerprint snippet."""

from __future__ import annotations

import hashlib
import re

from libref.astminer.queries import csharp as _csharp
from libref.astminer.queries import go as _go
from libref.astminer.queries import groovy as _groovy
from libref.astminer.queries import java as _java
from libref.astminer.queries import javascript as _javascript
from libref.astminer.queries import kotlin as _kotlin
from libref.astminer.queries import python as _python
from libref.astminer.queries import rust as _rust
from libref.astminer.queries import typescript as _typescript

QUERY_VERSION = 1

KINDS = ("entity", "call", "dependency", "comment")

# Capture naming contract: every @name in query text must satisfy the
# vocabulary below. Shape per domain:
#   entity.<category>[.signature.<attr> | .<role>...]
#   call.<category>[.<role>...]
#   dependency.<category>[.<role>...]
#   comment.line | comment.doc
ENTITY_CATEGORIES = frozenset(
    {
        "annotation",
        "arrow",
        "class",
        "constructor",
        "decorator",
        "enum",
        "enum_constant",
        "field",
        "function",
        "generator",
        "impl",
        "interface",
        "macro",
        "method",
        "module",
        "object_method",
        "package",
        "record",
        "struct",
        "type",
        "union",
        "variable",
    }
)

ENTITY_ROLES = frozenset(
    {
        "arguments",
        "base",
        "body",
        "call",
        "file_scoped",
        "name",
        "params",
        "receiver",
        "return_type",
        "source",
        # Struct field tag (for example Go); unrelated to any call category.
        "tag",
        "type",
        "value",
    }
)

SIGNATURE_ROLES = frozenset(
    {"base", "name", "params", "return_type", "source", "type", "type_params"}
)

CALL_CATEGORIES = frozenset(
    {
        "async",
        "constructor",
        "function",
        "hof",
        "macro",
        "method",
        "reference",
        "super",
    }
)

CALL_ROLES = frozenset(
    {
        "arguments",
        "callback",
        "chained",
        "class",
        "from",
        "function",
        "lambda",
        "method",
        "method_ref",
        "name",
        "object",
        "static",
        "super",
        "to",
    }
)

DEPENDENCY_CATEGORIES = frozenset(
    {
        "embedding",
        "export",
        "extend",
        "implement",
        "import",
        "module",
        "require",
        "use",
        "using",
    }
)

DEPENDENCY_ROLES = frozenset(
    {
        "alias",
        "callee",
        "default",
        "destruct",
        "dynamic",
        "exports",
        "from",
        "future",
        "import",
        "items",
        "list",
        "module",
        "name",
        "named",
        "namespace",
        "opens",
        "path",
        "provides",
        "relative",
        "requires",
        "spec",
        "static",
        "uses",
        "wildcard",
    }
)

COMMENT_CAPTURES = frozenset({"comment.line", "comment.doc"})

_CAPTURE_PATTERN = re.compile(r"@([A-Za-z_0-9.]+)")


def capture_parts(name: str) -> tuple[str, ...]:
    return tuple(name.split("."))


def capture_domain(name: str) -> str:
    parts = capture_parts(name)
    return parts[0] if parts else ""


def has_suffix(name: str, suffix: str) -> bool:
    want = tuple(seg for seg in suffix.split(".") if seg)
    if not want:
        return False
    parts = capture_parts(name)
    if len(parts) < len(want):
        return False
    return tuple(parts[-len(want) :]) == want


def has_alias_segment(name: str) -> bool:
    return "alias" in capture_parts(name)


def is_comment_doc(name: str) -> bool:
    return name == "comment.doc"


def is_comment_line(name: str) -> bool:
    return name == "comment.line"


def is_impl_root(name: str) -> bool:
    return name == "entity.impl"


def is_file_scoped_capture(name: str) -> bool:
    return name == "entity.package.file_scoped"


def is_base_name_capture(name: str) -> bool:
    return name in ("dependency.extend.name", "dependency.implement.name")


def is_entity_root(name: str) -> bool:
    parts = capture_parts(name)
    return len(parts) == 2 and parts[0] == "entity" and parts[1] in ENTITY_CATEGORIES


def is_signature_capture(name: str) -> bool:
    parts = capture_parts(name)
    return (
        len(parts) == 4
        and parts[0] == "entity"
        and parts[2] == "signature"
        and parts[3] in SIGNATURE_ROLES
    )


def _check_entity(parts: tuple[str, ...]) -> bool:
    if len(parts) < 2 or parts[1] not in ENTITY_CATEGORIES:
        return False
    if len(parts) == 2:
        return True
    if len(parts) == 3:
        return parts[2] in ENTITY_ROLES
    if len(parts) == 4:
        if parts[2] == "signature":
            return parts[3] in SIGNATURE_ROLES
        return parts[2] in ("call", "type") and parts[3] in ENTITY_ROLES
    return False


def _check_call(parts: tuple[str, ...]) -> bool:
    return (
        2 <= len(parts) <= 4
        and parts[1] in CALL_CATEGORIES
        and all(part in CALL_ROLES for part in parts[2:])
    )


def _check_dependency(parts: tuple[str, ...]) -> bool:
    return (
        2 <= len(parts) <= 4
        and parts[1] in DEPENDENCY_CATEGORIES
        and all(part in DEPENDENCY_ROLES for part in parts[2:])
    )


def is_known_capture(name: str) -> bool:
    parts = capture_parts(name)
    if not parts or len(parts) > 4:
        return False
    domain = parts[0]
    if domain == "entity":
        return _check_entity(parts)
    if domain == "call":
        return _check_call(parts)
    if domain == "dependency":
        return _check_dependency(parts)
    if domain == "comment":
        return name in COMMENT_CAPTURES
    return False


def validate_names(names) -> None:
    unknown = sorted({name for name in names if not is_known_capture(name)})
    if unknown:
        raise ValueError(f"unknown captures: {', '.join('@' + n for n in unknown)}")


def capture_names(language: str) -> set[str]:
    found: set[str] = set()
    for text in texts(language).values():
        found.update(_CAPTURE_PATTERN.findall(text))
    return found


def validate_captures(language: str) -> None:
    names = capture_names(language)
    try:
        validate_names(names)
    except ValueError as exc:
        raise ValueError(f"{language}: {exc}") from exc
    from libref.astminer.rules import entities as _entities

    handled = _entities.handled_entity_roots()
    unhandled = sorted(
        name for name in names if is_entity_root(name) and name not in handled
    )
    if unhandled:
        raise ValueError(
            f"{language}: entity roots without selection rule: "
            + ", ".join("@" + name for name in unhandled)
        )


_MODULES = {
    "python": _python,
    "java": _java,
    "csharp": _csharp,
    "go": _go,
    "javascript": _javascript,
    "typescript": _typescript,
    "rust": _rust,
    "kotlin": _kotlin,
    "groovy": _groovy,
}


def get(language: str, kind: str) -> str:
    module = _MODULES[language]
    return getattr(module, f"{kind}_query")()


def texts(language: str) -> dict[str, str]:
    return {kind: get(language, kind) for kind in KINDS}


def fingerprint_fragment(language: str) -> str:
    blob = "\x00".join([str(QUERY_VERSION)] + [texts(language)[kind] for kind in KINDS])
    return hashlib.sha1(blob.encode()).hexdigest()
