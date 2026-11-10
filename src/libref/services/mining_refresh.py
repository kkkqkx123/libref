"""Mining refresh: rebuild derived sequence patterns, trees, and clusters."""

from __future__ import annotations

from typing import Any

MINE_PATTERNS_JOB_KIND = "mine_patterns"
INDUCE_LABELS_JOB_KIND = "induce_labels"

DEFAULT_MINSUP = 2
DEFAULT_MAXLEN = 10
DEFAULT_MINLEN = 2


def _cluster_defaults(
    threshold: float | None, min_size: int | None
) -> tuple[float, int]:
    """When cluster params are missing, resolve from the settings-class defaults instead of copying numbers."""
    if threshold is not None and min_size is not None:
        return threshold, min_size
    from libref.config import LibrefSettings

    fields = LibrefSettings.model_fields
    resolved_threshold = (
        fields["cluster_threshold"].default if threshold is None else threshold
    )
    resolved_size = fields["cluster_min_size"].default if min_size is None else min_size
    return float(resolved_threshold), int(resolved_size)


def _clear_derived(conn, version_id: int) -> None:
    """Clear all derived mining outputs within a version, keeping the tables consistent when the call or sequence list is empty."""
    conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))
    if _label_table_ready(conn):
        conn.execute("DELETE FROM pattern_labels WHERE version_id=?", (version_id,))


def _review_map(conn, version_id: int) -> dict[int, str]:
    try:
        from libref.services.review import ReviewService

        return ReviewService(conn).review_map_for_version(version_id)
    except Exception:  # noqa: BLE001 — review map is advisory; mining proceeds unfiltered
        return {}


def _keep_call(confidence: str, status: str | None, include_ambiguous: bool) -> bool:
    if status == "conflict":
        return False
    if confidence != "AMBIGUOUS":
        return True
    if status == "approved":
        return True
    return bool(include_ambiguous)


def refresh_feature_tree(
    conn, library_id: int, version_id: int, *, include_ambiguous: bool = False
) -> int:
    """Rebuild hierarchical feature nodes in bulk; returns the node count."""
    from libref.mining import feature_tree as _tree
    from libref.mining import scope_key as _scope_key

    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence, id FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    review = _review_map(conn, version_id)
    rows = [
        r
        for r in rows
        if _keep_call(str(r[3]), review.get(int(r[4])), include_ambiguous)
    ]
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
    """Rebuild API clusters in bulk; returns the cluster count."""
    from libref.mining import build_scope_sets as _scope_sets
    from libref.mining.cluster import cluster_apis as _cluster_fn

    threshold, min_size = _cluster_defaults(threshold, min_size)

    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence, id FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    review = _review_map(conn, version_id)
    rows = [
        r
        for r in rows
        if _keep_call(str(r[3]), review.get(int(r[4])), include_ambiguous)
    ]
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
    review = _review_map(conn, version_id)
    rows = [
        r
        for r in rows
        if _keep_call(str(r[4]), review.get(int(r[7])), include_ambiguous)
    ]
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
        if _label_table_ready(conn):
            conn.execute(
                f"DELETE FROM pattern_labels WHERE version_id=?"
                f" AND pattern NOT IN ({placeholders})",
                (version_id, *wanted),
            )
    else:
        conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
        if _label_table_ready(conn):
            conn.execute("DELETE FROM pattern_labels WHERE version_id=?", (version_id,))
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
    """Build mining job params, symmetric with scrape job params, ensuring replayability."""
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
    """Build the mining job consumer function, handling only mine_patterns, decoupled from source indexing."""
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
            cluster_threshold=float(
                p.get(
                    "cluster_threshold",
                    _cluster_fields["cluster_threshold"].default,
                )
            ),
            cluster_min_size=int(
                p.get(
                    "cluster_min_size",
                    _cluster_fields["cluster_min_size"].default,
                )
            ),
        )

    return runner


def _label_table_ready(conn) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='pattern_labels'"
    ).fetchone()
    return bool(row and row[0])


def induce_job_params(
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    topn: int | None = None,
    include_ambiguous: bool = False,
) -> dict[str, Any]:
    """Build induction job params with replayable scope."""
    return {
        "library": library,
        "version": version,
        "ecosystem": ecosystem,
        "topn": topn,
        "include_ambiguous": include_ambiguous,
    }


async def induce_labels(
    conn,
    settings,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    topn: int | None = None,
    include_ambiguous: bool = False,
    chat=None,
) -> dict[str, Any]:
    """Induce names for top patterns with a chat function; disabled without a key."""
    import json as _json

    from libref.config import setting_or_default
    from libref.llm.errors import LlmConfigError
    from libref.mining.induction import (
        InductionInput,
        build_label_prompt,
        parse_label_payload,
        pattern_hash,
    )
    from libref.services.libraries import LibraryService
    from libref.services.mining_read import MiningReadService

    if not bool(setting_or_default(settings, "induction_enabled")):
        return {"disabled": True, "labeled": 0, "failed": 0, "skipped": 0}
    resolve_chat = chat
    if resolve_chat is None:
        try:
            from libref.services.factory import build_gateway

            resolve_chat = build_gateway(settings).chat_fn()
        except Exception:  # noqa: BLE001 -- missing key disables induction
            resolve_chat = None
    if resolve_chat is None:
        raise LlmConfigError("chat is not enabled: enable chat and set an api key")
    if not _label_table_ready(conn):
        return {"disabled": True, "reason": "missing table", "labeled": 0}
    catalog = LibraryService(conn)
    if ecosystem is not None:
        library_id, version_id = catalog.ensure_library_version(
            library, version, ecosystem
        )
    else:
        library_id, version_id = catalog.ensure_library_version(library, version)
    want_topn = (
        int(topn)
        if topn is not None
        else int(setting_or_default(settings, "induction_topn"))
    )
    prompt_version = str(setting_or_default(settings, "induction_prompt_version"))
    model = str(setting_or_default(settings, "chat_model"))
    batch_size = max(1, int(setting_or_default(settings, "induction_batch")))
    reader = MiningReadService(conn)
    patterns = reader.list_patterns(
        library, version, ecosystem, limit=max(1, want_topn)
    )
    labeled = 0
    failed = 0
    skipped = 0
    for entry in patterns:
        names = list(entry.get("pattern") or [])
        if not names:
            continue
        phash = pattern_hash(names)
        exists = conn.execute(
            "SELECT count(*) FROM pattern_labels WHERE version_id=? AND pattern_hash=? AND model=? AND prompt_version=?",
            (version_id, phash, model, prompt_version),
        ).fetchone()
        if exists and int(exists[0]) > 0:
            skipped += 1
            continue
        shapes: list[dict[str, Any]] = []
        siblings: list[str] = []
        for api in names:
            try:
                api_shapes = reader.arg_shapes(
                    api,
                    library,
                    version,
                    ecosystem,
                    limit=2,
                    include_ambiguous=include_ambiguous,
                )
            except Exception:  # noqa: BLE001 -- shapes are advisory for prompts
                api_shapes = []
            for shape in api_shapes:
                shapes.append(
                    {
                        "api": api,
                        "args": shape.get("args"),
                        "count": shape.get("count", 0),
                    }
                )
            try:
                api_related = reader.related_apis(
                    api,
                    library,
                    version,
                    ecosystem,
                    limit=4,
                    include_ambiguous=include_ambiguous,
                )
            except Exception:  # noqa: BLE001 -- siblings are advisory for prompts
                api_related = []
            for rel in api_related:
                text = str(rel.get("api") or "")
                if text and text not in siblings:
                    siblings.append(text)
        shapes = shapes[:6]
        siblings = siblings[:6]
        allowed = set(names) | set(siblings)
        scope = f"{library}@{version}" + (f"({ecosystem})" if ecosystem else "")
        item = InductionInput(
            pattern=names,
            support=int(entry.get("support", 1) or 1),
            arg_shapes=shapes,
            siblings=siblings,
            scope=scope,
        )
        try:
            import asyncio as _asyncio

            messages = build_label_prompt(item)
            produced = resolve_chat(messages)
            if _asyncio.iscoroutine(produced):
                produced = await produced
            label = parse_label_payload(str(produced), allowed)
        except Exception:  # noqa: BLE001 -- one bad reply must not block the batch
            failed += 1
            continue
        conn.execute(
            "INSERT INTO pattern_labels(library_id, version_id, pattern_hash, pattern, model,"
            " prompt_version, name, summary, category_path, workflow, related_apis)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(version_id, pattern_hash, model, prompt_version) DO UPDATE SET"
            " pattern=excluded.pattern, name=excluded.name, summary=excluded.summary,"
            " category_path=excluded.category_path, workflow=excluded.workflow,"
            " related_apis=excluded.related_apis, updated_at=datetime('now')",
            (
                library_id,
                version_id,
                phash,
                _json.dumps(names, ensure_ascii=False),
                model,
                prompt_version,
                label.name,
                label.summary,
                _json.dumps(label.category_path, ensure_ascii=False),
                label.workflow,
                _json.dumps(label.related_apis, ensure_ascii=False),
            ),
        )
        labeled += 1
        if labeled % batch_size == 0:
            conn.commit()
    conn.commit()
    return {"disabled": False, "labeled": labeled, "failed": failed, "skipped": skipped}


def build_induce_runner(settings, conn):
    """Build the induction job consumer."""

    async def runner(job):
        if job.kind != INDUCE_LABELS_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        return await induce_labels(
            conn,
            settings,
            p["library"],
            p["version"],
            ecosystem=p.get("ecosystem"),
            topn=p.get("topn"),
            include_ambiguous=bool(p.get("include_ambiguous", False)),
        )

    return runner
