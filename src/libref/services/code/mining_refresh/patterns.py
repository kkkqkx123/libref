"""Sequential pattern mining orchestration."""

from __future__ import annotations

from libref.services.code.mining_refresh.callset import _load_kept_calls
from libref.services.code.mining_refresh.derived import (
    _clear_derived,
    _delete_not_in,
    _refresh_doc_derived,
    refresh_clusters,
    refresh_facets,
    refresh_feature_tree,
)
from libref.services.code.mining_refresh.induction import _label_table_ready
from libref.services.code.mining_refresh.params import (
    DEFAULT_MAXLEN,
    DEFAULT_MINLEN,
    DEFAULT_MINSUP,
    validate_mining_params,
)


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
    include_unconfirmed: bool = False,
    cluster_threshold: float | None = None,
    cluster_min_size: int | None = None,
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
    profile: str | None = None,
    source_roles: list[str] | tuple[str, ...] | None = None,
    relation_kinds: list[str] | tuple[str, ...] | None = None,
    macro_policy: str | None = None,
    tenant_id: str = "default",
) -> list[dict]:
    validate_mining_params(minsup, maxlen, minlen, topn=topn)

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
    from libref.services.code.usage_sources import resolve_profile

    resolved_profile = resolve_profile(
        conn,
        version_id=version_id,
        name=profile,
        source_roles=source_roles,
        tenant_id=tenant_id,
    )
    profile_id = int(resolved_profile["id"])
    source_ids = list(resolved_profile.get("source_ids") or [])
    policy = str(macro_policy or resolved_profile.get("macro_policy") or "exclude")
    if policy == "exclude" and "macro" in (relation_kinds or ()):
        policy = "review_only"
    if resolved_profile.get("fingerprint_changed"):
        _clear_derived(conn, version_id, profile_id)
    rows = _load_kept_calls(
        conn,
        version_id,
        include_ambiguous=include_ambiguous,
        include_unconfirmed=include_unconfirmed,
        resolve_kinds=resolve_kinds,
        min_args=min_args,
        max_args=max_args,
        source_ids=source_ids,
        relation_kinds=relation_kinds,
        macro_policy=policy,
    )

    def _refresh_derived() -> None:
        refresh_feature_tree(
            conn, library_id, version_id, rows=rows, profile_id=profile_id
        )
        refresh_clusters(
            conn,
            library_id,
            version_id,
            rows=rows,
            threshold=cluster_threshold,
            min_size=cluster_min_size,
            profile_id=profile_id,
        )
        refresh_facets(conn, library_id, version_id, rows=rows, profile_id=profile_id)

    if not rows:
        _clear_derived(conn, version_id, profile_id)
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
        sample_id,
        _relation,
        _target_status,
        _args,
    ) in rows:
        key = str(sample_id) or _scope_key(str(caller_file), caller)
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

    def _empty() -> list[dict]:
        _clear_derived(conn, version_id, profile_id)
        _refresh_derived()
        conn.commit()
        _refresh_doc_derived(conn, library, version, profile, tenant_id, ecosystem)
        return []

    if not str_seqs:
        return _empty()
    if topn is not None:
        ranked = _freq.exact_counts([n for seq in str_seqs for n in seq])
        allowed = _freq.top_exact_names(ranked, topn)
        str_seqs = [[n for n in seq if n in allowed] for seq in str_seqs]
        str_seqs = [seq for seq in str_seqs if seq]
        if not str_seqs:
            return _empty()
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
        batch.append((library_id, version_id, profile_id, payload, int(support)))
        results.append({"pattern": names, "support": int(support), "is_closed": 1})
    if batch:
        conn.executemany(
            "INSERT INTO seq_patterns(library_id, version_id, profile_id, pattern,"
            " support, is_closed) VALUES(?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(profile_id, pattern) DO UPDATE SET support=excluded.support,"
            " is_closed=1",
            batch,
        )
    _delete_not_in(conn, "seq_patterns", version_id, profile_id, "pattern", wanted)
    if _label_table_ready(conn):
        _delete_not_in(
            conn, "pattern_labels", version_id, profile_id, "pattern", wanted
        )
    _refresh_derived()
    conn.commit()
    _refresh_doc_derived(conn, library, version, profile, tenant_id, ecosystem)
    results.sort(key=lambda r: (-r["support"], r["pattern"]))
    return results
