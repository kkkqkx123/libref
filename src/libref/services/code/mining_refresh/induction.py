"""LLM-backed label induction for patterns and clusters."""

from __future__ import annotations

from typing import Any


def _table_ready(conn, name: str) -> bool:
    if hasattr(conn, "table_columns"):
        return bool(conn.table_columns(name))
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 -- optional derived table on either backend
        return False
    return True


def _label_table_ready(conn) -> bool:
    return _table_ready(conn, "pattern_labels")


async def induce_labels(
    conn,
    settings,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    topn: int | None = None,
    include_ambiguous: bool = False,
    include_unconfirmed: bool = False,
    profile: str | None = None,
    macro_policy: str | None = None,
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
    from libref.services.code.usage_sources import (
        profile_source_context,
        resolve_profile,
    )

    resolved_profile = resolve_profile(
        conn, version_id=version_id, name=profile, tenant_id="default"
    )
    profile_id = int(resolved_profile["id"])
    profile_sources = list(resolved_profile.get("source_ids") or [])
    policy = str(macro_policy or resolved_profile.get("macro_policy") or "exclude")
    source_hint = profile_source_context(conn, profile_sources)["hint"]
    source_hint += f"; macro policy {policy}"
    if policy in ("review_only", "approved_only"):
        source_hint += (
            "; macro calls are visible in macro bodies, not expanded call sites"
        )
    want_topn = (
        int(topn)
        if topn is not None
        else int(setting_or_default(settings, "induction_topn"))
    )
    prompt_version = (
        str(setting_or_default(settings, "induction_prompt_version")) + ":" + policy
    )
    model = str(setting_or_default(settings, "chat_model"))
    batch_size = max(1, int(setting_or_default(settings, "induction_batch")))
    reader = MiningReadService(conn, settings)
    patterns = reader.list_patterns(
        library, version, ecosystem, limit=max(1, want_topn), profile=profile
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
            canonical = reader.canonical_signature(
                api_name, library, version, ecosystem
            )
        except Exception:  # noqa: BLE001, S112 -- signatures are advisory for prompts
            continue
        text = str(canonical.get("short") or "").strip()
        if text:
            short_sigs[str(api_name)] = text
    # One grouped shapes query for every API in the batch instead of one per API.
    wanted_apis = sorted(
        {a for entry in patterns for a in (entry.get("pattern") or []) if a}
    )
    try:
        siblings_by_api = reader.related_apis_batch(
            wanted_apis,
            library,
            version,
            ecosystem,
            limit=4,
            include_ambiguous=include_ambiguous,
        )
    except Exception:  # noqa: BLE001 -- siblings are advisory for prompts
        siblings_by_api = {a: [] for a in wanted_apis}
    prefetched: dict[str, list[dict[str, Any]]] = {a: [] for a in wanted_apis}
    if wanted_apis:
        from libref.services.registry.libraries import mining_version_ids

        scope_ids = mining_version_ids(conn, library, version, ecosystem)
        if scope_ids:
            base = "(rs.status IS NULL OR rs.status != 'conflict')"
            shape_filter = (
                base
                if include_unconfirmed
                else (
                    f"{base} AND call_instances.target_status != 'version_unconfirmed'"
                    if include_ambiguous
                    else (
                        f"{base} AND call_instances.target_status = 'resolved_target'"
                        " AND (call_instances.confidence != 'AMBIGUOUS'"
                        " OR rs.status='approved')"
                        " AND (COALESCE(call_instances.resolve_kind,'') NOT IN ('stdlib','builtin')"
                        " OR rs.status='approved')"
                    )
                )
            )
            if policy == "exclude":
                shape_filter += " AND COALESCE(relation_kind,'direct') != 'macro'"
            elif policy == "approved_only":
                shape_filter += (
                    " AND (COALESCE(relation_kind,'direct') != 'macro'"
                    " OR rs.status='approved')"
                )
            in_v = ",".join("?" for _ in scope_ids)
            in_a = ",".join("?" for _ in wanted_apis)
            shape_params: list[Any] = [*scope_ids, *wanted_apis]
            source_filter = ""
            if profile_sources:
                marks = ",".join("?" for _ in profile_sources)
                source_filter = f" AND source_id IN ({marks})"
                shape_params.extend(profile_sources)
            try:
                shape_rows = conn.execute(
                    "SELECT callee_name, args, count(*) FROM call_instances"
                    " LEFT JOIN review_status rs ON rs.entity_type='call_instance'"
                    " AND rs.entity_id=call_instances.id"
                    f" WHERE version_id IN ({in_v}) AND callee_name IN ({in_a})"
                    f" AND in_target_api=1 {source_filter} AND {shape_filter}"
                    " GROUP BY callee_name, args ORDER BY callee_name, 3 DESC",
                    shape_params,
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
            "SELECT count(*) FROM pattern_labels WHERE version_id=? AND profile_id=?"
            " AND pattern_hash=? AND model=? AND prompt_version=?",
            (version_id, profile_id, phash, model, prompt_version),
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
            for rel in siblings_by_api.get(api, []):
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
            source_hint=source_hint,
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
            "INSERT INTO pattern_labels(library_id, version_id, profile_id, pattern_hash,"
            " pattern, model, prompt_version, name, summary, category_path, workflow,"
            " related_apis) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(profile_id, pattern_hash, model, prompt_version) DO UPDATE SET"
            " pattern=excluded.pattern, name=excluded.name, summary=excluded.summary,"
            " category_path=excluded.category_path, workflow=excluded.workflow,"
            " related_apis=excluded.related_apis, updated_at=datetime('now')",
            (
                library_id,
                version_id,
                profile_id,
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
    clusters = await induce_cluster_labels(
        conn,
        settings,
        library,
        version,
        ecosystem=ecosystem,
        topn=topn,
        include_ambiguous=include_ambiguous,
        profile=profile,
        macro_policy=macro_policy,
        chat=resolve_chat,
    )
    return {
        "disabled": False,
        "labeled": labeled,
        "failed": failed,
        "skipped": skipped,
        "clusters": clusters,
    }


async def induce_cluster_labels(
    conn,
    settings,
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    topn: int | None = None,
    include_ambiguous: bool = False,
    profile: str | None = None,
    macro_policy: str | None = None,
    chat=None,
) -> dict[str, Any]:
    """Induce names for API groups; shares the gateway and idempotency shape."""
    import json as _json

    from libref.config import setting_or_default
    from libref.llm.errors import LlmConfigError
    from libref.mining.induction import (
        ClusterInductionInput,
        build_cluster_prompt,
        cluster_key,
        parse_cluster_payload,
    )
    from libref.services.code.mining_read import MiningReadService
    from libref.services.registry.libraries import LibraryService

    if not _table_ready(conn, "cluster_labels"):
        return {"disabled": True, "labeled": 0, "failed": 0, "skipped": 0}
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
    catalog = LibraryService(conn)
    if ecosystem is not None:
        library_id, version_id = catalog.ensure_library_version(
            library, version, ecosystem
        )
    else:
        library_id, version_id = catalog.ensure_library_version(library, version)
    from libref.services.code.usage_sources import (
        profile_source_context,
        resolve_profile,
    )

    resolved_profile = resolve_profile(
        conn, version_id=version_id, name=profile, tenant_id="default"
    )
    profile_id = int(resolved_profile["id"])
    policy = str(macro_policy or resolved_profile.get("macro_policy") or "exclude")
    profile_context = profile_source_context(
        conn, list(resolved_profile.get("source_ids") or [])
    )
    source_hint = str(profile_context.get("hint") or "")
    source_roles = [str(r) for r in (profile_context.get("roles") or [])]
    source_hint += f"; macro policy {policy}"
    want_topn = (
        int(topn)
        if topn is not None
        else int(setting_or_default(settings, "induction_topn"))
    )
    prompt_version = (
        str(setting_or_default(settings, "induction_prompt_version")) + ":" + policy
    )
    model = str(setting_or_default(settings, "chat_model"))
    batch_size = max(1, int(setting_or_default(settings, "induction_batch")))
    reader = MiningReadService(conn, settings)
    groups = reader.api_clusters(
        library, version, ecosystem, limit=max(1, want_topn), profile=profile
    )
    try:
        siblings_by_api = reader.related_apis_batch(
            {str(g.get("representative") or "") for g in groups},
            library,
            version,
            ecosystem,
            limit=4,
            include_ambiguous=include_ambiguous,
        )
    except Exception:  # noqa: BLE001 -- siblings are advisory for prompts
        siblings_by_api = {}
    entity_rows = conn.execute(
        "SELECT qualified_name FROM api_entities WHERE version_id=?", (version_id,)
    ).fetchall()
    entity_names = {str(r[0]) for r in entity_rows if r[0]}
    labeled = 0
    failed = 0
    skipped = 0
    done = 0
    for group in groups:
        members = sorted({str(m) for m in (group.get("members") or []) if m})
        if not members:
            continue
        key = cluster_key(members)
        exists = conn.execute(
            "SELECT count(*) FROM cluster_labels WHERE version_id=? AND profile_id=?"
            " AND cluster_key=? AND model=? AND prompt_version=?",
            (version_id, profile_id, key, model, prompt_version),
        ).fetchone()
        if exists and int(exists[0]) > 0:
            skipped += 1
            continue
        representative = str(
            group.get("representative") or (members[0] if members else "")
        )
        siblings: list[str] = []
        for rel in siblings_by_api.get(representative, []):
            text = str(rel.get("api") or "")
            if text and text not in siblings and text not in members:
                siblings.append(text)
        allowed = set(members) | set(siblings)
        scope = f"{library}@{version}" + (f"({ecosystem})" if ecosystem else "")
        item = ClusterInductionInput(
            members=members,
            representative=representative,
            size=int(group.get("size") or len(members)),
            siblings=siblings[:6],
            scope=scope,
            source_hint=source_hint,
            source_roles=source_roles,
        )
        try:
            import asyncio as _asyncio

            messages = build_cluster_prompt(item)
            produced = resolve_chat(messages)
            if _asyncio.iscoroutine(produced):
                produced = await produced
            label = parse_cluster_payload(str(produced), allowed, entity_names)
        except Exception:  # noqa: BLE001 -- one bad reply must not block the batch
            failed += 1
            continue
        conn.execute(
            "INSERT INTO cluster_labels(library_id, version_id, profile_id, cluster_key,"
            " members, model, prompt_version, name, summary, category_path, workflow,"
            " related_apis, source_note) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(profile_id, cluster_key, model, prompt_version) DO UPDATE SET"
            " members=excluded.members, name=excluded.name, summary=excluded.summary,"
            " category_path=excluded.category_path, workflow=excluded.workflow,"
            " related_apis=excluded.related_apis, source_note=excluded.source_note,"
            " updated_at=datetime('now')",
            (
                library_id,
                version_id,
                profile_id,
                key,
                _json.dumps(members, ensure_ascii=False),
                model,
                prompt_version,
                label.name,
                label.summary,
                _json.dumps(label.category_path, ensure_ascii=False),
                label.workflow,
                _json.dumps(label.related_apis, ensure_ascii=False),
                label.source_note,
            ),
        )
        labeled += 1
        done += 1
        if done % batch_size == 0:
            conn.commit()
    conn.commit()
    return {"disabled": False, "labeled": labeled, "failed": failed, "skipped": skipped}
