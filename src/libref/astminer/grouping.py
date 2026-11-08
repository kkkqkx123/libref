"""实体分组：类加成员、文件头加顶层项。

输入已抽取的声明列表与文件原文，输出源码位置有序的实体组，
供分块组装按成员边界切分。调用关系不参与分组。
"""

from __future__ import annotations

from dataclasses import dataclass, field

_CLASS_LIKE = frozenset({"class", "interface", "enum", "record", "annotation"})


@dataclass
class EntityGroup:
    group_id: str
    file: str
    kind: str
    name: str
    header_qual: str | None
    member_quals: list[str]
    header_span: tuple[int, int]
    member_spans: dict[str, tuple[int, int]]
    header_context: str = ""
    doc_members: dict[str, bool] = field(default_factory=dict)


def build_groups(decls, source: bytes, path: str) -> list[EntityGroup]:
    """按归属折叠声明：类锚点吸收直属后代，其余顶层声明各自成组。"""
    anchors = [d for d in decls if d.parent is None]
    anchors.sort(key=lambda d: (d.start_byte, -d.end_byte))
    children: dict[str, list] = {}
    for decl in decls:
        if decl.parent is not None:
            children.setdefault(decl.parent, []).append(decl)
    for members in children.values():
        members.sort(key=lambda d: (d.start_byte, -d.end_byte))
    groups: list[EntityGroup] = []
    first_start = min((d.start_byte for d in anchors), default=0)
    head_text = source[:first_start].decode("utf-8", "replace").strip()
    for index, anchor in enumerate(anchors):
        if anchor.kind in _CLASS_LIKE:
            members = list(children.get(anchor.qualified_name, []))
            member_quals = [m.qualified_name for m in members]
            spans = {m.qualified_name: (m.start_byte, m.end_byte) for m in members}
            docs = {m.qualified_name: bool(m.has_doc) for m in members}
            kind = anchor.kind
        else:
            member_quals = [anchor.qualified_name]
            spans = {anchor.qualified_name: (anchor.start_byte, anchor.end_byte)}
            docs = {anchor.qualified_name: bool(anchor.has_doc)}
            kind = anchor.kind
        groups.append(
            EntityGroup(
                group_id=f"{path}::{anchor.qualified_name}",
                file=path,
                kind=kind,
                name=anchor.qualified_name,
                header_qual=anchor.qualified_name,
                member_quals=member_quals,
                header_span=(anchor.start_byte, anchor.end_byte),
                member_spans=spans,
                header_context=head_text if index == 0 else "",
                doc_members=docs,
            )
        )
    return groups
