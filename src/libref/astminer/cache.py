"""增量缓存：仓库名、路径、指纹、配置版本联合主键。"""

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
        (repo, path, file_sha1, fp, cfg_version, json.dumps(records, ensure_ascii=False)),
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
            decls.append(
                ApiDeclRecord(
                    qualified_name=item["qualified_name"],
                    kind=item["kind"],
                    file=path,
                    line_start=int(item.get("line_start", 1)),
                    line_end=int(item.get("line_end", 1)),
                )
            )
        elif kind == "alias":
            aliases.append((item["alias"], item["full"]))
        elif kind == "raw_call":
            calls.append(
                RawCall(
                    raw_callee=item["raw"],
                    args=list(item.get("args", [])),
                    caller_file=path,
                    caller=item.get("caller"),
                    line_start=int(item["line_start"]),
                    line_end=int(item["line_end"]),
                )
            )
    return decls, aliases, calls


def sweep_repo(
    conn: sqlite3.Connection,
    repo: str,
    live: dict[str, tuple[str, int]],
) -> int:
    rows = conn.execute(
        "SELECT path, fp, cfg_version FROM ast_cache WHERE repo=?", (repo,)
    ).fetchall()
    dead: list[tuple[str, str, int]] = []
    for path, fp, cfg_version in rows:
        want = live.get(path)
        if want is None or want != (fp, int(cfg_version)):
            dead.append((path, fp, int(cfg_version)))
    for path, fp, cfg_version in dead:
        conn.execute(
            "DELETE FROM ast_cache WHERE repo=? AND path=? AND fp=? AND cfg_version=?",
            (repo, path, fp, cfg_version),
        )
    return len(dead)
