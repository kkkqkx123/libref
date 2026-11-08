"""模块路径上下文：相对路径转模块路径、相对导入展开、内外判定。"""

from __future__ import annotations


def determine_module_path(rel_path: str, language: str) -> str | None:
    path = rel_path.replace("\\", "/").strip("./")
    if language == "python":
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
        return path[: -len(".java")].replace("/", ".") or None
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


def is_internal_import(
    import_path: str,
    language: str,
    package: str | None = None,
    project_packages: tuple[str, ...] = (),
) -> bool:
    if language == "python" and import_path.startswith("."):
        return True
    for prefix in project_packages:
        if prefix and import_path.startswith(prefix):
            return True
    if package and language == "java" and import_path.startswith(package):
        rest = import_path[len(package):]
        if not rest or rest.startswith("."):
            return True
    return False
