"""Incremental cache: composite key of repo name, path, fingerprint, and config version."""

from __future__ import annotations

import json
import sqlite3

from libref.astminer.models import ApiDeclRecord, RawCall


def lookup(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    fp: str,
    cfg_version: int,
) -> tuple[str, list[dict]] | None:
    row = conn.execute(
        "SELECT file_sha1, records FROM ast_cache"
        " WHERE repo=? AND path=? AND fp=? AND cfg_version=?",
        (repo, path, fp, cfg_version),
    ).fetchone()
    if row is None:
        return None
    return row[0], [dict(r) for r in json.loads(row[1])]


def store(
    conn: sqlite3.Connection,
    repo: str,
    path: str,
    file_sha1: str,
    fp: str,
    cfg_version: int,
    records: list[dict],
) -> None:
    conn.execute(
        "INSERT INTO ast_cache(repo, path, file_sha1, fp, cfg_version, records)"
        " VALUES(?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(repo, path, fp, cfg_version)"
        " DO UPDATE SET file_sha1=excluded.file_sha1, records=excluded.records",
        (
            repo,
            path,
            file_sha1,
            fp,
            cfg_version,
            json.dumps(records, ensure_ascii=False),
        ),
    )


def encode_records(
    decls: list[ApiDeclRecord],
    aliases: list[tuple[str, str]],
    calls: list[RawCall],
) -> list[dict]:
    records: list[dict] = []
    for decl in decls:
        records.append(
            {
                "type": "decl",
                "qualified_name": decl.qualified_name,
                "kind": decl.kind,
                "line_start": decl.line_start,
                "line_end": decl.line_end,
                "start_byte": decl.start_byte,
                "end_byte": decl.end_byte,
                "signature": decl.signature,
                "params": list(decl.params),
                "param_types": list(decl.param_types),
                "return_type": decl.return_type,
                "bases": list(decl.bases),
                "parent": decl.parent,
                "depth": decl.depth,
                "has_doc": bool(decl.has_doc),
                "span_kind": decl.span_kind,
            }
        )
    for alias, full in aliases:
        records.append({"type": "alias", "alias": alias, "full": full})
    for call in calls:
        records.append(
            {
                "type": "raw_call",
                "raw": call.raw_callee,
                "args": list(call.args),
                "line_start": call.line_start,
                "line_end": call.line_end,
                "caller": call.caller,
                "start_byte": call.start_byte,
                "end_byte": call.end_byte,
                "receiver": call.receiver,
                "func": call.func,
                "argument_count": call.argument_count,
            }
        )
    return records


def decode_records(
    records: list[dict], path: str
) -> tuple[list[ApiDeclRecord], list[tuple[str, str]], list[RawCall]]:
    decls: list[ApiDeclRecord] = []
    aliases: list[tuple[str, str]] = []
    calls: list[RawCall] = []
    for item in records:
        kind = item.get("type")
        if kind == "decl":
            if not item.get("qualified_name") or not item.get("kind"):
                raise ValueError("cached decl is missing required identity")
            if "line_start" not in item or "line_end" not in item:
                raise ValueError("cached decl is missing required lines")
            decls.append(
                ApiDeclRecord(
                    qualified_name=item["qualified_name"],
                    kind=item["kind"],
                    file=path,
                    line_start=int(item.get("line_start", 1)),
                    line_end=int(item.get("line_end", 1)),
                    start_byte=int(item.get("start_byte", 0)),
                    end_byte=int(item.get("end_byte", 0)),
                    signature=str(item.get("signature", "")),
                    params=list(item.get("params", [])),
                    param_types=list(item.get("param_types", [])),
                    return_type=str(item.get("return_type", "")),
                    bases=list(item.get("bases", [])),
                    parent=item.get("parent"),
                    depth=int(item.get("depth", 0)),
                    has_doc=bool(item.get("has_doc", False)),
                    span_kind=str(item.get("span_kind", "exact_entities"))
                    or "exact_entities",
                )
            )
        elif kind == "alias":
            if not item.get("alias") or not item.get("full"):
                raise ValueError("cached alias is missing required fields")
            aliases.append((item["alias"], item["full"]))
        elif kind == "raw_call":
            if not item.get("raw") or "line_start" not in item or "line_end" not in item:
                raise ValueError("cached call is missing required fields")
            calls.append(
                RawCall(
                    raw_callee=item["raw"],
                    args=list(item.get("args", [])),
                    caller_file=path,
                    caller=item.get("caller"),
                    line_start=int(item["line_start"]),
                    line_end=int(item["line_end"]),
                    start_byte=int(item.get("start_byte", 0)),
                    end_byte=int(item.get("end_byte", 0)),
                    receiver=item.get("receiver"),
                    func=str(item.get("func", "") or item["raw"].rpartition(".")[2]),
                    argument_count=int(
                        item.get("argument_count", len(item.get("args", [])))
                    ),
                )
            )
        else:
            raise ValueError(f"unknown cached record type: {kind!r}")
    return decls, aliases, calls


def sweep_repo(
    conn: sqlite3.Connection,
    repo: str,
    live: dict[str, tuple[str, int]],
) -> int:
    rows = conn.execute(
        "SELECT path, fp, cfg_version FROM ast_cache WHERE repo=?", (repo,)
    ).fetchall()
    dead = [
        (path, fp, int(cfg_version))
        for path, fp, cfg_version in rows
        if live.get(path) != (fp, int(cfg_version))
    ]
    for start in range(0, len(dead), 200):
        chunk = dead[start : start + 200]
        conn.executemany(
            "DELETE FROM ast_cache WHERE repo=? AND path=? AND fp=? AND cfg_version=?",
            [(repo, path, fp, cfg_version) for path, fp, cfg_version in chunk],
        )
    return len(dead)
