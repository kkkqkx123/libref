"""Manifest parsing: module identity and declared external dependencies for Go, Rust, and C#.

Pure in-memory parsing over small manifest text; no database dependency. The
ingest orchestration calls this once per repository root and persists the
result as version-scoped relation edges.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel

MANIFEST_FILES = (
    "go.mod",
    "Cargo.toml",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
)


class ManifestDep(BaseModel):
    ecosystem: str
    name: str
    req: str = ""
    source_file: str = ""


def _read_text(root: Path, filename: str) -> str | None:
    try:
        return (Path(root) / filename).read_text(encoding="utf-8")
    except OSError:
        return None


def _go_module_name(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module"):
            rest = stripped[len("module") :].strip().strip("\"'")
            return rest.split()[0] if rest else None
    return None


def _go_requires(text: str) -> list[tuple[str, str]]:
    """Require entries: single-line and block forms; comments and blank lines skipped."""
    out: list[tuple[str, str]] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        body = line
        if not in_block:
            if not body.startswith("require "):
                continue
            body = body[len("require ") :].strip().strip("()").strip()
        parts = body.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


def _cargo_package_name(text: str) -> str | None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ always ships tomllib
        return None
    try:
        data = tomllib.loads(text)
    except ValueError:
        return None
    package = data.get("package") if isinstance(data, dict) else None
    if isinstance(package, dict):
        name = str(package.get("name") or "").strip()
        return name or None
    return None


def _cargo_requires(text: str) -> list[tuple[str, str]]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ always ships tomllib
        return []
    try:
        data = tomllib.loads(text)
    except ValueError:
        return []
    out: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        return []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            req = ""
            if isinstance(spec, str):
                req = spec.strip()
            elif isinstance(spec, dict):
                req = str(spec.get("version") or "").strip()
            if name and name not in (n for n, _ in out):
                out.append((str(name), req))
    targets = data.get("target")
    if isinstance(targets, dict):
        for per_target in targets.values():
            if not isinstance(per_target, dict):
                continue
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                deps = per_target.get(section)
                if not isinstance(deps, dict):
                    continue
                for name, spec in deps.items():
                    req = (
                        spec.strip()
                        if isinstance(spec, str)
                        else str(
                            (spec.get("version") if isinstance(spec, dict) else "")
                            or ""
                        ).strip()
                    )
                    if name and name not in (n for n, _ in out):
                        out.append((str(name), req))
    return out


def _csproj_files(root: Path) -> list[Path]:
    try:
        return sorted(Path(root).rglob("*.csproj"))
    except OSError:
        return []


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _csproj_identity(text: str, stem: str) -> str | None:
    try:
        xml_root = ET.fromstring(text)
    except ET.ParseError:
        return None
    for node in xml_root.iter():
        if (
            _local(node.tag) in ("PackageId", "AssemblyName")
            and node.text
            and node.text.strip()
        ):
            return node.text.strip()
    return stem or None


def _csproj_requires(text: str) -> list[tuple[str, str]]:
    try:
        xml_root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[tuple[str, str]] = []
    for node in xml_root.iter():
        if _local(node.tag) != "PackageReference":
            continue
        name = (node.get("Include") or node.get("Update") or "").strip()
        version = (node.get("Version") or "").strip()
        if not version:
            for child in node:
                if _local(child.tag) == "Version" and child.text and child.text.strip():
                    version = child.text.strip()
                    break
        if name and name not in (n for n, _ in out):
            out.append((name, version))
    return out


def _package_json_identity(text: str) -> str | None:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    name = str(data.get("name") or "").strip()
    return name or None


def _package_json_requires(text: str) -> list[tuple[str, str]]:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            req = str(spec or "").strip().lstrip("^~>=< \t")
            if name and str(name) not in [seen for seen, _ in out]:
                out.append((str(name), req))
    return out


def _pep508_name(spec: str) -> str:
    import re

    head = re.split(r"[\s\[;><=!~]+", spec.strip(), maxsplit=1)[0]
    return head.strip()


def _pyproject_identity(text: str) -> str | None:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ always ships tomllib
        return None
    try:
        data = tomllib.loads(text)
    except ValueError:
        return None
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return None
    name = str(project.get("name") or "").strip()
    return name or None


def _pyproject_requires(text: str) -> list[tuple[str, str]]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ always ships tomllib
        return []
    try:
        data = tomllib.loads(text)
    except ValueError:
        return []
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return []
    out: list[tuple[str, str]] = []
    deps = project.get("dependencies")
    if isinstance(deps, list):
        for spec in deps:
            name = _pep508_name(str(spec))
            if name and name not in [seen for seen, _ in out]:
                out.append((name, str(spec).strip()))
    return out


def _requirements_requires(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        line = line.split("#", 1)[0].strip().split(";", 1)[0].strip()
        if not line or "://" in line or line.startswith("."):
            continue
        name = _pep508_name(line)
        if name and name not in [seen for seen, _ in out]:
            out.append((name, line[len(name) :].strip()))
    return out


def _pom_text(node) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _pom_identity(text: str) -> str | None:
    try:
        xml_root = ET.fromstring(text)
    except ET.ParseError:
        return None
    group = _pom_text(xml_root.find("{*}groupId"))
    artifact = _pom_text(xml_root.find("{*}artifactId"))
    if not group:
        parent = xml_root.find("{*}parent")
        if parent is not None:
            group = _pom_text(parent.find("{*}groupId"))
    if not artifact or not group:
        return None
    return f"{group}:{artifact}"


def _pom_requires(text: str) -> list[tuple[str, str]]:
    try:
        xml_root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for deps in xml_root.iter():
        if _local(deps.tag) != "dependencies":
            continue
        for dep in list(deps):
            if _local(dep.tag) != "dependency":
                continue
            group = _pom_text(dep.find("{*}groupId"))
            artifact = _pom_text(dep.find("{*}artifactId"))
            version = _pom_text(dep.find("{*}version"))
            if _pom_text(dep.find("{*}scope")) == "system":
                continue
            if group and artifact and f"{group}:{artifact}" not in seen:
                seen.add(f"{group}:{artifact}")
                out.append((f"{group}:{artifact}", version))
    return out


def manifest_identity(root) -> tuple[str, str] | None:
    """Module identity from the first manifest found: go.mod, Cargo.toml, package.json, pyproject.toml, pom.xml, then csproj."""
    base = Path(root)
    go_text = _read_text(base, "go.mod")
    if go_text is not None:
        name = _go_module_name(go_text)
        if name:
            return ("go", name)
    cargo_text = _read_text(base, "Cargo.toml")
    if cargo_text is not None:
        name = _cargo_package_name(cargo_text)
        if name:
            return ("crates", name)
    package_text = _read_text(base, "package.json")
    if package_text is not None:
        name = _package_json_identity(package_text)
        if name:
            try:
                from libref.crawler.registry import normalize_name

                name = normalize_name("npm", name)
            except ValueError:
                pass
            return ("npm", name)
    pyproject_text = _read_text(base, "pyproject.toml")
    if pyproject_text is not None:
        name = _pyproject_identity(pyproject_text)
        if name:
            try:
                from libref.crawler.registry import normalize_name

                name = normalize_name("pypi", name)
            except ValueError:
                pass
            return ("pypi", name)
    pom_text = _read_text(base, "pom.xml")
    if pom_text is not None:
        name = _pom_identity(pom_text)
        if name:
            return ("maven", name)
    for csproj in _csproj_files(base)[:1]:
        try:
            text = csproj.read_text(encoding="utf-8")
        except OSError:
            continue
        name = _csproj_identity(text, csproj.stem)
        if name:
            try:
                from libref.crawler.registry.nuget import normalize_nuget_name

                name = normalize_nuget_name(name)
            except ValueError:
                pass
            return ("nuget", name)
    return None


def manifest_dependencies(root) -> list[ManifestDep]:
    """Declared external dependencies across the three manifests; names already registry-normalized."""
    base = Path(root)
    out: list[ManifestDep] = []

    def _push(ecosystem: str, name: str, req: str, source_file: str) -> None:
        norm = normalize_dep_name(ecosystem, name)
        if norm and norm not in [d.name for d in out if d.ecosystem == ecosystem]:
            out.append(
                ManifestDep(
                    ecosystem=ecosystem, name=norm, req=req, source_file=source_file
                )
            )

    go_text = _read_text(base, "go.mod")
    if go_text is not None:
        for name, req in _go_requires(go_text):
            _push("go", name, req, "go.mod")
    cargo_text = _read_text(base, "Cargo.toml")
    if cargo_text is not None:
        for name, req in _cargo_requires(cargo_text):
            _push("crates", name, req, "Cargo.toml")
    package_text = _read_text(base, "package.json")
    if package_text is not None:
        for name, req in _package_json_requires(package_text):
            _push("npm", name, req, "package.json")
    pyproject_text = _read_text(base, "pyproject.toml")
    if pyproject_text is not None:
        for name, req in _pyproject_requires(pyproject_text):
            _push("pypi", name, req, "pyproject.toml")
    requirements_text = _read_text(base, "requirements.txt")
    if requirements_text is not None:
        for name, req in _requirements_requires(requirements_text):
            _push("pypi", name, req, "requirements.txt")
    pom_text = _read_text(base, "pom.xml")
    if pom_text is not None:
        for name, req in _pom_requires(pom_text):
            _push("maven", name, req, "pom.xml")
    for csproj in _csproj_files(base):
        try:
            rel = csproj.relative_to(base).as_posix()
        except ValueError:
            rel = csproj.name
        try:
            text = csproj.read_text(encoding="utf-8")
        except OSError:
            continue
        for name, req in _csproj_requires(text):
            _push("nuget", name, req, rel)
    return out


def normalize_dep_name(ecosystem: str, name: str) -> str:
    """Registry-identity normalization for dependency edges; unknown ecosystems pass through."""
    text = name.strip()
    if ecosystem == "go":
        return text
    if ecosystem == "crates":
        return text.lower()
    if ecosystem == "nuget":
        return text.lower()
    if ecosystem == "pypi":
        import re

        return re.sub(r"[-_.]+", "-", text).lower()
    if ecosystem == "npm":
        return text.lower()
    if ecosystem == "maven":
        try:
            from libref.crawler.registry.maven import normalize_maven_name

            return normalize_maven_name(text)
        except ValueError:
            return ""
    return text
