"""Maven Central provider: coordinate normalization plus metadata and POM parsing.

The Maven-specific part of the registry subpackage; shared URL sanitization and
version logic live in the subpackage facade. Coordinates take the form
group:artifact, with case preserved to keep repository paths correct.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from libref.crawler.registry import is_repo_host_url, sanitize_http_url


def normalize_maven_name(name: str) -> str:
    """Coordinate normalization: trim whitespace; trim group and artifact separately; missing colon is a usage error."""
    text = name.strip()
    if ":" not in text:
        raise ValueError(f"maven name must look like group:artifact: {name!r}")
    group, _, artifact = text.partition(":")
    group = group.strip()
    artifact = artifact.strip()
    if not group or not artifact:
        raise ValueError(f"maven name must look like group:artifact: {name!r}")
    if "/" in group or "/" in artifact or " " in group or " " in artifact:
        raise ValueError(f"invalid maven coordinates: {name!r}")
    if artifact.count(":") or group.count(":"):
        raise ValueError(f"invalid maven coordinates: {name!r}")
    return f"{group}:{artifact}"


def split_maven_name(name: str) -> tuple[str, str]:
    group, _, artifact = normalize_maven_name(name).partition(":")
    return group, artifact


def maven_metadata_url(base: str, group: str, artifact: str) -> str:
    path = group.replace(".", "/")
    return f"{base.rstrip('/')}/{path}/{artifact}/maven-metadata.xml"


def maven_pom_url(base: str, group: str, artifact: str, version: str) -> str:
    path = group.replace(".", "/")
    return f"{base.rstrip('/')}/{path}/{artifact}/{version}/{artifact}-{version}.pom"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _first_text(root: ET.Element, names: tuple[str, ...]) -> str | None:
    for node in root.iter():
        if _local(node.tag) in names and node.text and node.text.strip():
            return node.text.strip()
    return None


def parse_maven_metadata(xml_text: str) -> tuple[str | None, list[str]]:
    """Parse the version list and stable version; return empty when versioning is missing."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, []
    versions: list[str] = []
    for node in root.iter():
        if _local(node.tag) == "version" and node.text and node.text.strip():
            # Only collect version entries under versioning; version elements in
            # dependency declarations share the tag but differ in structure. Parent-chain
            # checks would be too convoluted; all version elements in the metadata file
            # are released versions, so collect them directly.
            versions.append(node.text.strip())
    # Deduplicate, preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for v in versions:
        if v not in seen:
            seen.add(v)
            ordered.append(v)
    stable: str | None = None
    for key in ("release", "latest"):
        text = _first_text(root, (key,))
        if text:
            stable = text
            break
    return stable, ordered


def _clean_scm_url(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().strip("<>\"'")
    if text.startswith("scm:"):
        # scm:git:https://... — take the last recognizable protocol segment
        idx = text.find("https://")
        jdx = text.find("http://")
        if idx >= 0:
            text = text[idx:]
        elif jdx >= 0:
            text = text[jdx:]
        else:
            text = text.split(":")[-1]
    return text or None


def rank_maven_candidates(
    project_url: str | None,
    *,
    allow_private: bool = False,
) -> list[str]:
    out: list[str] = []
    clean = sanitize_http_url(project_url, allow_private=allow_private)
    if clean and not is_repo_host_url(clean):
        out.append(clean)
    return out


def parse_maven_pom(
    xml_text: str, *, allow_private: bool = False
) -> tuple[str | None, list[str], str | None]:
    """Parse the project URL and repo URL; the repo URL is a hint only and never a candidate."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, [], None
    project_url: str | None = None
    for node in root.iter():
        # Project URL is the top-level url; urls under scm are handled separately
        if _local(node.tag) == "url" and node.text and node.text.strip():
            project_url = node.text.strip()
            break
    scm_url: str | None = None
    in_scm = False
    for node in root.iter():
        tag = _local(node.tag)
        if tag == "scm":
            in_scm = True
            for child in list(node):
                if _local(child.tag) == "url" and child.text and child.text.strip():
                    scm_url = _clean_scm_url(child.text)
                    break
            break
    _ = in_scm
    homepage = sanitize_http_url(project_url, allow_private=allow_private)
    candidates = rank_maven_candidates(project_url, allow_private=allow_private)
    repo_url = sanitize_http_url(_clean_scm_url(scm_url), allow_private=allow_private)
    return homepage, candidates, repo_url
