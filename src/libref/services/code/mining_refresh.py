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
    from libref.services.ops.review import ReviewService

    return ReviewService(conn).review_map_for_version(version_id)


def _keep_call(confidence: str, status: str | None, include_ambiguous: bool) -> bool:
    if status == "conflict":
        return False
    if confidence != "AMBIGUOUS":
        return True
    if status == "approved":
        return True
    return bool(include_ambiguous)


def _load_kept_calls(
    conn,
    version_id: int,
    *,
    include_ambiguous: bool = False,
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
):
    """Single shared scan with review plus optional mining filters."""
    wanted = sorted({str(k) for k in (resolve_kinds or []) if str(k).strip()})
    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence, id,"
        " line_start, call_order, start_byte, resolve_kind, argument_count"
        " FROM call_instances WHERE version_id=?",
        (version_id,),
    ).fetchall()
    review = _review_map(conn, version_id)
    out = []
    for r in rows:
        if not _keep_call(str(r[3]), review.get(int(r[4])), include_ambiguous):
            continue
        if wanted and str(r[8] or "") not in wanted:
            continue
        try:
            argc = int(r[9] or 0)
        except (TypeError, ValueError):
            argc = 0
        if min_args is not None and argc < int(min_args):
            continue
        if max_args is not None and argc > int(max_args):
            continue
        out.append(r)
    return out


def _delete_not_in(conn, table: str, version_id: int, column: str, wanted: list) -> int:
    """Set-difference cleanup; returns the removed row count.

    The live set is read in one pass and the dead ids are deleted in bounded
    batches. An empty wanted set clears the version.
    """
    if not wanted:
        cur = conn.execute(f"DELETE FROM {table} WHERE version_id=?", (version_id,))
        return int(cur.rowcount or 0)
    keep = set(wanted)
    existing = [
        r[0]
        for r in conn.execute(
            f"SELECT {column} FROM {table} WHERE version_id=?", (version_id,)
        ).fetchall()
    ]
    dead = [v for v in existing if v not in keep]
    removed = 0
    for start in range(0, len(dead), 500):
        batch = dead[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        cur = conn.execute(
            f"DELETE FROM {table} WHERE version_id=? AND {column} IN ({placeholders})",
            (version_id, *batch),
        )
        removed += int(cur.rowcount or 0)
    return removed


def refresh_feature_tree(
    conn,
    library_id: int,
    version_id: int,
    *,
    include_ambiguous: bool = False,
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
) -> int:
    """Rebuild hierarchical feature nodes in bulk; returns the node count."""
    from libref.mining import feature_tree as _tree
    from libref.mining import scope_key as _scope_key

    rows = _load_kept_calls(
        conn,
        version_id,
        include_ambiguous=include_ambiguous,
        resolve_kinds=resolve_kinds,
        min_args=min_args,
        max_args=max_args,
    )
    calls = [(str(r[0]), _scope_key(str(r[1]), r[2])) for r in rows if r[0]]
    if not calls:
        conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
        return 0
    entity_rows = conn.execute(
        "SELECT qualified_name, kind, parent FROM api_entities WHERE version_id=?",
        (version_id,),
    ).fetchall()
    kinds = {str(r[0]): str(r[1]) for r in entity_rows}
    parents = {str(r[0]): (str(r[2]) if r[2] else None) for r in entity_rows}
    nodes = _tree.build_feature_tree(calls, kinds, parents)
    wanted = [n["path"] for n in nodes]
    conn.executemany(
        "INSERT INTO feature_nodes(library_id, version_id, path, parent, depth, kind, calls, scopes)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(version_id, path) DO UPDATE SET parent=excluded.parent,"
        " depth=excluded.depth, kind=excluded.kind,"
        " calls=excluded.calls, scopes=excluded.scopes",
        [
            (
                library_id,
                version_id,
                node["path"],
                node["parent"],
                int(node["depth"]),
                node["kind"],
                int(node["calls"]),
                int(node["scopes"]),
            )
            for node in nodes
        ],
    )
    _delete_not_in(conn, "feature_nodes", version_id, "path", wanted)
    return len(nodes)


def refresh_clusters(
    conn,
    library_id: int,
    version_id: int,
    *,
    include_ambiguous: bool = False,
    threshold: float | None = None,
    min_size: int | None = None,
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
) -> int:
    """Rebuild API clusters in bulk; returns the cluster count."""
    from libref.mining import build_scope_sets as _scope_sets
    from libref.mining.cluster import cluster_apis as _cluster_fn

    threshold, min_size = _cluster_defaults(threshold, min_size)

    rows = _load_kept_calls(
        conn,
        version_id,
        include_ambiguous=include_ambiguous,
        resolve_kinds=resolve_kinds,
        min_args=min_args,
        max_args=max_args,
    )
    triplets = [(str(r[0]), str(r[1]), r[2]) for r in rows if r[0]]
    sets = _scope_sets(triplets)
    groups = _cluster_fn(sets, threshold=threshold, min_size=min_size)
    wanted = sorted({m for g in groups for m in g["members"]})
    if not wanted:
        conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))
        return 0
    conn.executemany(
        "INSERT INTO api_clusters(library_id, version_id, cluster_id, member, size, representative)"
        " VALUES(?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(version_id, member) DO UPDATE SET cluster_id=excluded.cluster_id,"
        " size=excluded.size, representative=excluded.representative",
        [
            (
                library_id,
                version_id,
                int(group["cluster_id"]),
                member,
                int(group["size"]),
                group["representative"],
            )
            for group in groups
            for member in group["members"]
        ],
    )
    _delete_not_in(conn, "api_clusters", version_id, "member", wanted)
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
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
) -> list[dict]:
    import json as _json

    from libref.mining import PrefixSpan
    from libref.mining import frequency as _freq
    from libref.mining import normalize as _norm
    from libref.mining import scope_key as _scope_key
    from libref.services.registry.libraries import LibraryService

    catalog = LibraryService(conn)
    if ecosystem is not None:
        library_id, version_id = catalog.ensure_library_version(
            library, version, ecosystem
        )
    else:
        library_id, version_id = catalog.ensure_library_version(library, version)
    rows = _load_kept_calls(
        conn,
        version_id,
        include_ambiguous=include_ambiguous,
        resolve_kinds=resolve_kinds,
        min_args=min_args,
        max_args=max_args,
    )
    if not rows:
        _clear_derived(conn, version_id)
        conn.commit()
        return []
    groups: dict[str, list[tuple[int, int, int, int, str]]] = {}
    for (
        callee,
        caller_file,
        caller,
        _conf,
        row_id,
        line_start,
        order,
        start,
        _rk,
        _argc,
    ) in rows:
        if caller is None or not str(caller).strip():
            continue
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
        _clear_derived(conn, version_id)
        refresh_feature_tree(
            conn,
            library_id,
            version_id,
            include_ambiguous=include_ambiguous,
            resolve_kinds=resolve_kinds,
            min_args=min_args,
            max_args=max_args,
        )
        refresh_clusters(
            conn,
            library_id,
            version_id,
            include_ambiguous=include_ambiguous,
            threshold=cluster_threshold,
            min_size=cluster_min_size,
            resolve_kinds=resolve_kinds,
            min_args=min_args,
            max_args=max_args,
        )
        conn.commit()
        return []
    if topn is not None:
        ranked = _freq.exact_counts([n for seq in str_seqs for n in seq])
        allowed = _freq.top_exact_names(ranked, topn)
        str_seqs = [[n for n in seq if n in allowed] for seq in str_seqs]
        str_seqs = [seq for seq in str_seqs if seq]
        if not str_seqs:
            _clear_derived(conn, version_id)
            refresh_feature_tree(
                conn,
                library_id,
                version_id,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
            )
            refresh_clusters(
                conn,
                library_id,
                version_id,
                include_ambiguous=include_ambiguous,
                threshold=cluster_threshold,
                min_size=cluster_min_size,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
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
    batch: list[tuple] = []
    for support, pattern in mined or []:
        names = _norm.decode_pattern(list(pattern), reverse)
        payload = _json.dumps(names, ensure_ascii=False)
        wanted.append(payload)
        batch.append((library_id, version_id, payload, int(support)))
        results.append({"pattern": names, "support": int(support), "is_closed": 1})
    if batch:
        conn.executemany(
            "INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)"
            " VALUES(?, ?, ?, ?, 1)"
            " ON CONFLICT(version_id, pattern) DO UPDATE SET support=excluded.support,"
            " is_closed=1",
            batch,
        )
    _delete_not_in(conn, "seq_patterns", version_id, "pattern", wanted)
    if _label_table_ready(conn):
        _delete_not_in(conn, "pattern_labels", version_id, "pattern", wanted)
    refresh_feature_tree(
        conn,
        library_id,
        version_id,
        include_ambiguous=include_ambiguous,
        resolve_kinds=resolve_kinds,
        min_args=min_args,
        max_args=max_args,
    )
    refresh_clusters(
        conn,
        library_id,
        version_id,
        include_ambiguous=include_ambiguous,
        threshold=cluster_threshold,
        min_size=cluster_min_size,
        resolve_kinds=resolve_kinds,
        min_args=min_args,
        max_args=max_args,
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
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
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
        "resolve_kinds": list(resolve_kinds) if resolve_kinds else None,
        "min_args": min_args,
        "max_args": max_args,
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
            resolve_kinds=p.get("resolve_kinds"),
            min_args=p.get("min_args"),
            max_args=p.get("max_args"),
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
    from libref.services.code.mining_read import MiningReadService
    from libref.services.registry.libraries import LibraryService

    if not bool(setting_or_default(settings, "induction_enabled")):
        return {"disabled": True, "labeled": 0, "failed": 0, "skipped": 0}
    resolve_chat = chat
    if resolve_chat is None:
        try:
            from libref.services.infra.vectors import build_gateway

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
    try:
        sig_table = reader.signatures_for(
            sorted(
                {a for entry in patterns for a in (entry.get("pattern") or []) if a}
            ),
            library,
            version,
            ecosystem,
        )
    except Exception:  # noqa: BLE001 -- signatures are advisory for prompts
        sig_table = {}
    short_sigs: dict[str, str] = {}
    for api_name in sorted(sig_table):
        try:
            canonical = reader.canonical_signature(api_name, library, version, ecosystem)
        except Exception:  # noqa: BLE001 -- signatures are advisory for prompts
            continue
        text = str(canonical.get("short") or "").strip()
        if text:
            short_sigs[str(api_name)] = text
    # One grouped shapes query for every API in the batch instead of one per API.
    wanted_apis = sorted(
        {a for entry in patterns for a in (entry.get("pattern") or []) if a}
    )
    prefetched: dict[str, list[dict[str, Any]]] = {a: [] for a in wanted_apis}
    if wanted_apis:
        from libref.services.registry.libraries import mining_version_ids

        scope_ids = mining_version_ids(conn, library, version, ecosystem)
        if scope_ids:
            base = "(rs.status IS NULL OR rs.status != 'conflict')"
            shape_filter = (
                base
                if include_ambiguous
                else (
                    f"{base} AND (call_instances.confidence != 'AMBIGUOUS'"
                    " OR rs.status='approved')"
                )
            )
            in_v = ",".join("?" for _ in scope_ids)
            in_a = ",".join("?" for _ in wanted_apis)
            try:
                shape_rows = conn.execute(
                    "SELECT callee_name, args, count(*) FROM call_instances"
                    " LEFT JOIN review_status rs ON rs.entity_type='call_instance'"
                    " AND rs.entity_id=call_instances.id"
                    f" WHERE version_id IN ({in_v}) AND callee_name IN ({in_a})"
                    f" AND {shape_filter}"
                    " GROUP BY callee_name, args ORDER BY callee_name, 3 DESC",
                    (*scope_ids, *wanted_apis),
                ).fetchall()
            except Exception:  # noqa: BLE001 -- shapes are advisory for prompts
                shape_rows = []
            for api_name, raw, count in shape_rows:
                slot = prefetched.get(str(api_name))
                if slot is None or len(slot) >= 2:
                    continue
                try:
                    parsed = _json.loads(raw) if raw else []
                except ValueError:
                    parsed = [raw]
                slot.append({"args": parsed, "count": int(count)})
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
            for shape in prefetched.get(api, []):
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
            signatures={k: short_sigs[k] for k in names if k in short_sigs},
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
