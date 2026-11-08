"""模块路径上下文：相对路径转模块路径、相对导入展开。"""

from __future__ import annotations

_JAVA_SOURCE_ROOTS = (
    "src/main/java/",
    "src/test/java/",
    "src/main/",
    "src/test/",
    "src/",
    "main/java/",
    "java/",
)

_JS_SUFFIXES = (".js", ".mjs", ".cjs")


def _clean_path(rel_path: str) -> str | None:
    path = rel_path.replace("\\", "/").removeprefix("./")
    path = path.lstrip("/")
    if not path or path == "." or path == "..":
        return None
    if path.startswith("../") or "/../" in path:
        return None
    return path


def determine_module_path(rel_path: str, language: str) -> str | None:
    path = _clean_path(rel_path)
    if path is None:
        return None
    if language == "python":
        if path == "__init__.py":
            return None
        if path.endswith("/__init__.py"):
            path = path[: -len("/__init__.py")]
        elif path.endswith(".py"):
            path = path[: -len(".py")]
        else:
            return None
        return path.replace("/", ".") or None
    if language == "java":
        if not path.endswith(".java"):
            return None
        path = path[: -len(".java")]
        for root in _JAVA_SOURCE_ROOTS:
            if path.startswith(root):
                path = path[len(root):]
                break
        return path.replace("/", ".") or None
    if language == "javascript":
        for suffix in _JS_SUFFIXES:
            if path.endswith(suffix):
                stem = path[: -len(suffix)]
                break
        else:
            return None
        if stem == "index":
            return None
        stem = stem.removesuffix("/index")
        return stem.replace("/", ".") or None
    return None


def resolve_python_relative(current_module: str | None, relative: str) -> str | None:
    if current_module is None:
        return None
    dots = len(relative) - len(relative.lstrip("."))
    if dots == 0:
        return None
    parts = current_module.split(".")
    for _ in range(dots):
        if parts:
            parts.pop()
    rest = relative.lstrip(".")
    if rest:
        parts.append(rest)
    return ".".join(parts) or None
