"""Repository collection: read files, reuse parse cache, extract declarations."""

from __future__ import annotations


def _read_go_module_prefix(root) -> str:
    from pathlib import Path

    try:
        text = (Path(root) / "go.mod").read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module"):
            rest = stripped[len("module") :].strip().strip("\"'")
            return rest.split()[0] if rest else ""
    return ""


def _collect_decl_index(
    conn,
    repo_name: str,
    root,
    files: list[str],
) -> tuple[
    list,
    dict[str, list[tuple[str, str]]],
    dict[str, int],
    dict[str, tuple[str, int]],
]:
    """Declaration pass: parse or reuse cache, keep only decls and aliases.

    Raw calls stay in the cache and are reloaded per batch, so peak memory
    grows with one batch instead of the whole repository."""
    import hashlib
    from pathlib import Path

    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine
    from libref.astminer import rules as ast_rules

    go_prefix = _read_go_module_prefix(root).replace("/", ".")
    all_decls: list = []
    file_aliases: dict[str, list[tuple[str, str]]] = {}
    counts = {"files": 0, "parsed": 0, "cached": 0}
    live: dict[str, tuple[str, int]] = {}
    for rel in files:
        counts["files"] += 1
        cfg = ast_config.config_for_suffix(Path(rel).suffix)
        if cfg is None:
            continue
        fp = ast_config.grammar_fingerprint(cfg)
        live[rel] = (fp, cfg.config_version)
        data = (root / rel).read_bytes()
        sha = hashlib.sha1(data).hexdigest()
        hit = ast_cache.lookup(conn, repo_name, rel, fp, cfg.config_version)
        if hit is not None and hit[0] == sha:
            counts["cached"] += 1
            decls, aliases, _raw = ast_cache.decode_records(hit[1], rel)
        else:
            counts["parsed"] += 1
            decls, aliases, raw_calls = ast_engine.extract_file(rel, cfg, data)
            ast_cache.store(
                conn,
                repo_name,
                rel,
                sha,
                fp,
                cfg.config_version,
                ast_cache.encode_records(decls, aliases, raw_calls),
            )
        all_decls.extend(decls)
        if go_prefix and cfg.name == "go":
            aliases = [
                (name, ast_rules.normalize_go_import(target, go_prefix))
                for name, target in aliases
            ]
        file_aliases[rel] = list(aliases)
    return all_decls, file_aliases, counts, live


def _load_raw_batch(
    conn,
    repo_name: str,
    root,
    files: list[str],
) -> list:
    """Reload one batch of raw calls from the declaration pass cache."""
    import hashlib
    from pathlib import Path

    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine

    raw_calls: list = []
    for rel in files:
        cfg = ast_config.config_for_suffix(Path(rel).suffix)
        if cfg is None:
            continue
        fp = ast_config.grammar_fingerprint(cfg)
        data = (root / rel).read_bytes()
        sha = hashlib.sha1(data).hexdigest()
        hit = ast_cache.lookup(conn, repo_name, rel, fp, cfg.config_version)
        if hit is not None and hit[0] == sha:
            _decls, _aliases, raw = ast_cache.decode_records(hit[1], rel)
            raw_calls.extend(raw)
            continue
        _decls, _aliases, raw = ast_engine.extract_file(rel, cfg, data)
        raw_calls.extend(raw)
        ast_cache.store(
            conn,
            repo_name,
            rel,
            sha,
            fp,
            cfg.config_version,
            ast_cache.encode_records(_decls, _aliases, raw),
        )
    return raw_calls
