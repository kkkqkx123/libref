"""Pure extraction of public re-export facts from library source text.

These facts bridge documentation names such as ``httpx.get`` or ``z.string()``
to the internal declarations that own the implementation. Only names written
by the source are emitted; no ecosystem-wide guessing is performed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from libref.astminer.context import determine_module_path

_JS_STAR_RE = re.compile(r'^\s*export\s+\*\s+from\s+["\']([^"\']+)["\']', re.MULTILINE)
_JS_NAMESPACE_RE = re.compile(
    r'^\s*export\s+\*\s+as\s+([A-Za-z_$][\w$]*)\s+from\s+["\']([^"\']+)["\']',
    re.MULTILINE,
)
_JS_NAMED_RE = re.compile(
    r'^\s*export\s*\{([^}]*)\}\s*from\s+["\']([^"\']+)["\']', re.MULTILINE
)
_PY_STAR_RE = re.compile(r"^\s*from\s+(\.+[\w.]*)\s+import\s+\*", re.MULTILINE)
_PY_ALL_RE = re.compile(r"^\s*__all__\s*=\s*(\[[^\]]*\]|\([^)]*\))", re.MULTILINE)
_RS_USE_RE = re.compile(r"^\s*pub\s+use\s+([^;]+);", re.MULTILINE)


@dataclass(frozen=True)
class ExportFact:
    """One public alias and the internal target it names."""

    alias: str
    target: str
    kind: str


def _relative_module(base: str, specifier: str, language: str) -> str:
    if language in ("javascript", "typescript"):
        spec = re.sub(r"\.(?:js|jsx|ts|tsx|mjs|cjs)$", "", specifier.strip())
        if not spec.startswith("."):
            return spec.replace("/", ".")
        parts = base.split(".") if base else []
        for part in spec.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return ".".join(parts)
    if language == "python":
        dots = len(specifier) - len(specifier.lstrip("."))
        rest = specifier[dots:]
        parts = base.split(".") if base else []
        for _ in range(max(0, dots - 1)):
            if parts:
                parts.pop()
        if rest:
            parts.extend(rest.split("."))
        return ".".join(parts)
    if language == "rust":
        parts = base.split(".")
        if "src" in parts:
            parts = parts[: parts.index("src") + 1]
        parts.extend(specifier.replace("::", ".").split("."))
        return ".".join(part for part in parts if part)
    return ""


def _js_named_names(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in body.split(","):
        parts = re.split(r"\s+as\s+", item.strip(), maxsplit=1)
        name = parts[0].strip()
        if not re.match(r"^[A-Za-z_$][\w$]*$", name):
            continue
        alias = parts[1].strip() if len(parts) == 2 else name
        out.append((name, alias))
    return out


def _rust_names(body: str) -> list[tuple[str, str]]:
    body = body.strip()
    entries = (
        body[1:-1].split(",") if body.startswith("{") and body.endswith("}") else [body]
    )
    names: list[tuple[str, str]] = []
    for item in entries:
        parts = re.split(r"\s+as\s+", item.strip(), maxsplit=1)
        name = parts[0].strip()
        if not re.match(r"^[A-Za-z_][\w]*$", name):
            continue
        names.append((name, parts[1].strip() if len(parts) == 2 else name))
    return names


def export_facts(path: str, language: str, source: bytes | str) -> list[ExportFact]:
    """Public aliases declared by one source file, with their internal targets."""
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    module = determine_module_path(path, language) or ""
    if not module:
        return []
    filename = path.replace("\\", "/").rpartition("/")[2]
    if language in ("javascript", "typescript"):
        base = (
            module if filename.split(".")[0] == "index" else module.rpartition(".")[0]
        )
    elif language == "python":
        base = (
            module if filename.startswith("__init__.py") else module.rpartition(".")[0]
        )
    else:
        base = module
    out: list[ExportFact] = []
    if language in ("javascript", "typescript"):
        namespace = {
            (match.group(1), _relative_module(base, match.group(2), language))
            for match in _JS_NAMESPACE_RE.finditer(text)
        }
        for alias, target in sorted(namespace):
            out.append(ExportFact(f"{module}.{alias}.*", target, "js_namespace"))
        stars = {
            _relative_module(base, match.group(1), language)
            for match in _JS_STAR_RE.finditer(text)
        } - {target for _alias, target in namespace}
        for target in sorted(stars):
            out.append(ExportFact(f"{module}.*", target, "js_star"))
        for match in _JS_NAMED_RE.finditer(text):
            target_module = _relative_module(base, match.group(2), language)
            for name, alias in _js_named_names(match.group(1)):
                out.append(
                    ExportFact(
                        f"{module}.{alias}", f"{target_module}.{name}", "js_named"
                    )
                )
    elif language == "python":
        stars = {
            _relative_module(base, match.group(1), language)
            for match in _PY_STAR_RE.finditer(text)
        }
        for target in sorted(stars):
            out.append(ExportFact(f"{module}.*", target, "python_star"))
        all_match = _PY_ALL_RE.search(text)
        if all_match is not None:
            try:
                names = ast.literal_eval(all_match.group(1))
            except SyntaxError, ValueError:
                names = []
            if isinstance(names, (list, tuple)):
                for name in names:
                    if isinstance(name, str) and name.isidentifier():
                        out.append(
                            ExportFact(
                                f"{module}.{name}", f"{module}.{name}", "python_all"
                            )
                        )
    elif language == "rust":
        for match in _RS_USE_RE.finditer(text):
            body = match.group(1).strip()
            path, separator, tail = body.partition("::")
            if not separator:
                continue
            target_module = _relative_module(base, path, language)
            for name, alias in _rust_names(tail):
                out.append(
                    ExportFact(
                        f"{module}.{alias}",
                        f"{target_module}.{name}",
                        "rust_pub_use",
                    )
                )
    return out
