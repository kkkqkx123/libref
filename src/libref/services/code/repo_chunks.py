"""Source chunk storage over the documents table."""

from __future__ import annotations


def _sweep_source_pages(conn, repo_name: str, live: set[str]) -> list[int]:
    """Delete source pages whose files vanished; returns removed document ids."""
    prefix = f"repo://{repo_name}/"
    rows = conn.execute(
        "SELECT id, url FROM pages WHERE url LIKE ? ESCAPE '\\'",
        (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
    ).fetchall()
    removed: list[int] = []
    for page_id, url in rows:
        rel = str(url)[len(prefix) :]
        if rel in live:
            continue
        row = conn.execute(
            "SELECT version_id FROM pages WHERE id=?", (page_id,)
        ).fetchone()
        if row is None:
            continue
        docs = conn.execute(
            "SELECT id FROM documents WHERE page_id=?", (page_id,)
        ).fetchall()
        removed.extend(int(r[0]) for r in docs)
        conn.execute("DELETE FROM pages WHERE id=?", (page_id,))
    return removed


def _store_source_chunks(
    conn,
    repo_name: str,
    root,
    live_files: list[str],
    all_decls,
    library_id: int,
    version_id: int,
    budgets=None,
) -> tuple[int, int, list[int], list[int]]:
    """Chunk source code and store it: assemble grouped by file, reuse the documents table for storing; returns chunk count, empty chunks, delete/insert ids."""
    from libref.astminer import grouping as _grouping
    from libref.astminer import source_chunk as _source_chunk
    from libref.chunking import default_budgets
    from libref.store.document_store import DocumentStore

    resolved = budgets or default_budgets()
    by_file: dict[str, list] = {}
    for decl in all_decls:
        by_file.setdefault(decl.file, []).append(decl)
    store = DocumentStore(conn)
    n_chunks = 0
    dropped = 0
    removed_all: list[int] = []
    inserted_all: list[int] = []
    for rel in sorted(live_files):
        decls = by_file.get(rel, [])
        if not decls:
            continue
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        groups = _grouping.build_groups(decls, data, rel)
        by_qual = {d.qualified_name: d for d in decls}
        split = _source_chunk.build_source_chunks(
            groups, by_qual, data, rel, budgets=resolved, repo=repo_name
        )
        dropped += split.empty_dropped
        page_url = f"repo://{repo_name}/{rel}"
        raw_chunks = [_source_chunk.as_chunk(c, page_url) for c in split.chunks]
        if not raw_chunks:
            continue
        for order, chunk in enumerate(raw_chunks):
            chunk.sort_order = order
        from libref.store.document_store import DocumentInput

        inputs = [
            DocumentInput(
                url=c.url,
                path=c.path,
                title=c.title,
                content=c.content,
                sort_order=c.sort_order,
                chunk_path=getattr(c, "chunk_path", "embedding"),
                source_group_id=getattr(c, "source_group_id", ""),
                chunk_id=getattr(c, "chunk_id", ""),
                span_kind=getattr(c, "span_kind", "document_range"),
                merged_from=list(getattr(c, "merged_from", []) or []),
                overlap_from=getattr(c, "overlap_from", None),
                content_hash=getattr(c, "content_hash", ""),
                keywords=list(getattr(c, "keywords", []) or []),
                content_type=getattr(c, "content_type", "text/markdown"),
                start_char=getattr(c, "start_char", -1),
                end_char=getattr(c, "end_char", -1),
                prev_chunk_id=getattr(c, "prev_chunk_id", None),
                next_chunk_id=getattr(c, "next_chunk_id", None),
                entities=list(getattr(c, "entities", []) or []),
            )
            for c in raw_chunks
        ]
        result = store.store_page(
            version_id,
            library_id,
            page_url,
            "stored",
            chunks=inputs,
            raw_content=data.decode("utf-8", "replace"),
        )
        removed_all.extend(result.removed_ids)
        inserted_all.extend(result.inserted_ids)
        n_chunks += len(inputs)
    return n_chunks, dropped, removed_all, inserted_all
