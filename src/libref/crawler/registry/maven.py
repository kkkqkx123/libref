"""Maven Central 提供方：坐标归一化加元数据加 POM 解析。

归属 registry 子包的 Maven 专属部分，通用地址卫生与版本逻辑在子包门面。
坐标形态为 group:artifact，大小写保留以保证仓库路径正确。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from libref.crawler.registry import is_repo_host_url, sanitize_http_url


def normalize_maven_name(name: str) -> str:
    """坐标归一化：去首尾空白，组与件分别去空白，缺冒号视为用法错误。"""
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
    """解析版本列表与稳定版，缺 versioning 时返回空。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, []
    versions: list[str] = []
    for node in root.iter():
        if _local(node.tag) == "version" and node.text and node.text.strip():
            # 只收 versioning 下的版本条目，依赖声明中的 version 同名但结构不同；
            # 按父链判定过于繁琐，元数据文件内 version 全部为发布版本，可直接收。
            versions.append(node.text.strip())
    # 去重保序
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
        # scm:git:https://... 取最后一个可识别协议段
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
    """解析项目地址与仓库地址，仓库地址只作提示不入候选。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, [], None
    project_url: str | None = None
    for node in root.iter():
        # 项目地址取顶层 url，scm 下的 url 另行处理
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
