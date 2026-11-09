"""Mining refresh: rebuild derived sequence patterns, trees, and clusters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

MINE_PATTERNS_JOB_KIND = "mine_patterns"

DEFAULT_MINSUP = 2
DEFAULT_MAXLEN = 10
DEFAULT_MINLEN = 2


def _cluster_defaults(threshold: float | None, min_size: int | None) -> tuple[float, int]:
    """集群参数缺省时按配置类默认值求解，不复制数值。"""
    if threshold is not None and min_size is not None:
        return threshold, min_size
    from libref.config import LibrefSettings

    fields = LibrefSettings.model_fields
    resolved_threshold = fields["cluster_threshold"].default if threshold is None else threshold
    resolved_size = fields["cluster_min_size"].default if min_size is None else min_size
    return float(resolved_threshold), int(resolved_size)


def _clear_derived(conn, version_id: int) -> None:
    """清空版本内全部派生挖掘产出，调用为空或序列为空时保持三表一致。"""
    conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))


def refresh_feature_tree(
    conn, library_id: int, version_id: int, *, include_ambiguous: bool = False
) -> int:
    """批量重建分层特征节点，返回节点数。"""
    from libref.mining import feature_tree as _tree
    from libref.mining import scope_key as _scope_key

    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    if not include_ambiguous:
        rows = [r for r in rows if r[3] != "AMBIGUOUS"]
    calls = [(str(r[0]), _scope_key(str(r[1]), r[2])) for r in rows if r[0]]
    if not calls:
        conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
        return 0
    kinds = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT qualified_name, kind FROM api_entities WHERE version_id=?",
            (version_id,),
        ).fetchall()
    }
    nodes = _tree.build_feature_tree(calls, kinds)
    wanted = [n["path"] for n in nodes]
    for node in nodes:
        conn.execute(
            "INSERT INTO feature_nodes(library_id, version_id, path, parent, depth, kind, calls, scopes)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(version_id, path) DO UPDATE SET parent=excluded.parent,"
            " depth=excluded.depth, kind=excluded.kind,"
            " calls=excluded.calls, scopes=excluded.scopes",
            (
                library_id,
                version_id,
                node["path"],
                node["parent"],
                int(node["depth"]),
                node["kind"],
                int(node["calls"]),
                int(node["scopes"]),
            ),
        )
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        conn.execute(
            "DELETE FROM feature_nodes WHERE version_id=?"
            f" AND path NOT IN ({placeholders})",
            (version_id, *wanted),
        )
    else:
        conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
    return len(nodes)


def refresh_clusters(
    conn,
    library_id: int,
    version_id: int,
    *,
    include_ambiguous: bool = False,
    threshold: float | None = None,
    min_size: int | None = None,
) -> int:
    """批量重建接口分组，返回分组数。"""
    from libref.mining import build_scope_sets as _scope_sets
    from libref.mining.cluster import cluster_apis as _cluster_fn

    threshold, min_size = _cluster_defaults(threshold, min_size)

    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    if not include_ambiguous:
        rows = [r for r in rows if r[3] != "AMBIGUOUS"]
    triplets = [(str(r[0]), str(r[1]), r[2]) for r in rows if r[0]]
    sets = _scope_sets(triplets)
    groups = _cluster_fn(sets, threshold=threshold, min_size=min_size)
    wanted = sorted({m for g in groups for m in g["members"]})
    if not wanted:
        conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))
        return 0
    for group in groups:
        for member in group["members"]:
            conn.execute(
                "INSERT INTO api_clusters(library_id, version_id, cluster_id, member, size, representative)"
                " VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(version_id, member) DO UPDATE SET cluster_id=excluded.cluster_id,"
                " size=excluded.size, representative=excluded.representative",
                (
                    library_id,
                    version_id,
                    int(group["cluster_id"]),
                    member,
                    int(group["size"]),
                    group["representative"],
                ),
            )
    placeholders = ",".join("?" for _ in wanted)
    conn.execute(
        "DELETE FROM api_clusters WHERE version_id=?"
        f" AND member NOT IN ({placeholders})",
        (version_id, *wanted),
    )
    return len(groups)


def mine_patterns(
    conn,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    minsup: int = DEFAULT_MINSUP,
    topn: int | None = None,
    maxlen: int = DEFAULT_MAXLEN,
    minlen: int = DEFAULT_MINLEN,
    include_ambiguous: bool = False,
    cluster_threshold: float | None = None,
    cluster_min_size: int | None = None,
) -> list[dict]:
    import json as _json

    from libref.mining import PrefixSpan
    from libref.mining import frequency as _freq
    from libref.mining import normalize as _norm
    from libref.mining import scope_key as _scope_key
    from libref.services.libraries import LibraryService

    catalog = LibraryService(conn)
    if ecosystem is not None:
        library_id, version_id = catalog.ensure_library_version(
            library, version, ecosystem
        )
    else:
        library_id, version_id = catalog.ensure_library_version(library, version)
    rows = conn.execute(
        "SELECT callee_name, caller_file, line_start, caller, confidence,"
        " call_order, start_byte, id FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    if not rows:
        _clear_derived(conn, version_id)
        conn.commit()
        return []
    if not include_ambiguous:
        rows = [r for r in rows if r[4] != "AMBIGUOUS"]
        if not rows:
            _clear_derived(conn, version_id)
            conn.commit()
            return []
    groups: dict[str, list[tuple[int, int, int, int, str]]] = {}
    for callee, caller_file, line_start, caller, _conf, order, start, row_id in rows:
        key = _scope_key(str(caller_file), caller)
        groups.setdefault(key, []).append(
            (int(line_start), int(start), int(order), int(row_id), str(callee))
        )
    str_seqs = [[name for *_key, name in sorted(items)] for items in groups.values()]
    cleaned: list[list[str]] = []
    for seq in str_seqs:
        seq = [name for name in seq if name]
        if len(seq) < 2:
            continue
        if len(seq) > 1000:
            seq = seq[:1000]
        cleaned.append(seq)
    str_seqs = cleaned
    if not str_seqs:
        conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
        refresh_feature_tree(
            conn, library_id, version_id, include_ambiguous=include_ambiguous
        )
        refresh_clusters(
            conn,
            library_id,
            version_id,
            include_ambiguous=include_ambiguous,
            threshold=cluster_threshold,
            min_size=cluster_min_size,
        )
        conn.commit()
        return []
    if topn is not None:
        ranked = _freq.exact_counts([n for seq in str_seqs for n in seq])
        allowed = _freq.top_exact_names(ranked, topn)
        str_seqs = [[n for n in seq if n in allowed] for seq in str_seqs]
        str_seqs = [seq for seq in str_seqs if seq]
        if not str_seqs:
            conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
            refresh_feature_tree(
                conn, library_id, version_id, include_ambiguous=include_ambiguous
            )
            refresh_clusters(
                conn,
                library_id,
                version_id,
                include_ambiguous=include_ambiguous,
                threshold=cluster_threshold,
                min_size=cluster_min_size,
            )
            conn.commit()
            return []
    table = _norm.build_table([n for seq in str_seqs for n in seq])
    reverse = _norm.reverse_table(table)
    db = _norm.encode_sequences(str_seqs, table)
    miner = PrefixSpan(db)
    miner.minlen = minlen
    miner.maxlen = maxlen
    mined = miner.frequent(minsup, closed=True)
    results: list[dict] = []
    wanted: list[str] = []
    for support, pattern in mined or []:
        names = _norm.decode_pattern(list(pattern), reverse)
        payload = _json.dumps(names, ensure_ascii=False)
        wanted.append(payload)
        conn.execute(
            "INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)"
            " VALUES(?, ?, ?, ?, 1)"
            " ON CONFLICT(version_id, pattern) DO UPDATE SET support=excluded.support,"
            " is_closed=1",
            (library_id, version_id, payload, int(support)),
        )
        results.append({"pattern": names, "support": int(support), "is_closed": 1})
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        conn.execute(
            f"DELETE FROM seq_patterns WHERE version_id=?"
            f" AND pattern NOT IN ({placeholders})",
            (version_id, *wanted),
        )
    else:
        conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
    refresh_feature_tree(
        conn, library_id, version_id, include_ambiguous=include_ambiguous
    )
    refresh_clusters(
        conn,
        library_id,
        version_id,
        include_ambiguous=include_ambiguous,
        threshold=cluster_threshold,
        min_size=cluster_min_size,
    )
    conn.commit()
    results.sort(key=lambda r: (-r["support"], r["pattern"]))
    return results


def mine_job_params(
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    minsup: int = DEFAULT_MINSUP,
    topn: int | None = None,
    maxlen: int = DEFAULT_MAXLEN,
    minlen: int = DEFAULT_MINLEN,
    include_ambiguous: bool = False,
    cluster_threshold: float | None = None,
    cluster_min_size: int | None = None,
) -> dict[str, Any]:
    """构造挖掘任务参数，与抓取任务参数对称，保证可重放。"""
    return {
        "library": library,
        "version": version,
        "ecosystem": ecosystem,
        "minsup": minsup,
        "topn": topn,
        "maxlen": maxlen,
        "minlen": minlen,
        "include_ambiguous": include_ambiguous,
        "cluster_threshold": cluster_threshold,
        "cluster_min_size": cluster_min_size,
    }


def build_mine_runner(settings, conn):
    """构造挖掘任务消费函数，只处理 mine_patterns，与源码索引解耦。"""
    from libref.config import LibrefSettings

    _cluster_fields = LibrefSettings.model_fields

    async def runner(job):
        if job.kind != MINE_PATTERNS_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        return mine_patterns(
            conn,
            p["library"],
            p["version"],
            ecosystem=p.get("ecosystem"),
            minsup=int(p.get("minsup", DEFAULT_MINSUP)),
            topn=p.get("topn"),
            maxlen=int(p.get("maxlen", DEFAULT_MAXLEN)),
            minlen=int(p.get("minlen", DEFAULT_MINLEN)),
            include_ambiguous=bool(p.get("include_ambiguous", False)),
            cluster_threshold=float(p.get(
                "cluster_threshold",
                _cluster_fields["cluster_threshold"].default,
            )),
            cluster_min_size=int(p.get(
                "cluster_min_size",
                _cluster_fields["cluster_min_size"].default,
            )),
        )

    return runner


