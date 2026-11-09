"""Review export: per-file parsing and segmentation reports written to disk, for manual confirmation against source.

Usage: uv run python tests/e2e/export_review.py [--lang javascript]
Reuses existing extraction, disambiguation, grouping, and chunking entry points; adds no extraction logic.
Output goes to tests/e2e/outputs/{lang}/ as human-readable text, not persisted to the DB, not used in assertions.
Chunking does no natural-language transformation; only file markers and segment spans are recorded.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent / "fixtures" / "manifest.json"
ROOT = Path(__file__).resolve().parent.parent.parent
OUT_ROOT = Path(__file__).resolve().parent / "outputs"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="export e2e parsing and chunk review")
    parser.add_argument("--lang", action="append", default=None)
    parser.add_argument("--commit", default="")
    return parser.parse_args()


def _line_of(source: bytes, offset: int) -> int:
    return source.count(b"\n", 0, offset) + 1


def export_repo(lang: str, root: Path, repo: str, commit: str,
                sample: list[str] | None = None) -> list[str]:
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine
    from libref.astminer import grouping as ast_grouping
    from libref.astminer import resolution as ast_resolution
    from libref.astminer import source_chunk as ast_source_chunk
    from libref.astminer.repos import iter_source_files

    out_dir = OUT_ROOT / lang
    out_dir.mkdir(parents=True, exist_ok=True)
    files = [r for r in iter_source_files(root, ast_config.all_suffixes())]
    if sample is not None:
        wanted = set(sample)
        files = [r for r in files if r in wanted]
    all_decls, file_aliases, all_raw = [], {}, []
    sources: dict[str, bytes] = {}
    for rel in files:
        cfg = ast_config.config_for_suffix(Path(rel).suffix)
        if cfg is None:
            continue
        data = (root / rel).read_bytes()
        sources[rel] = data
        decls, aliases, raw = ast_engine.extract_file(rel, cfg, data)
        all_decls.extend(decls)
        file_aliases[rel] = list(aliases)
        all_raw.extend(raw)
    resolved = ast_resolution.resolve_calls(all_decls, file_aliases, all_raw, repo, commit)
    by_file_calls: dict[str, list] = {}
    for call in resolved:
        by_file_calls.setdefault(call.caller_file, []).append(call)
    by_file_decls: dict[str, list] = {}
    for decl in all_decls:
        by_file_decls.setdefault(decl.file, []).append(decl)
    summary: list[str] = [f"repo={repo} commit={commit} files={len(sources)}"]
    for rel in sorted(sources):
        data = sources[rel]
        lines = [f"# File: {rel}", ""]
        decls = sorted(
            by_file_decls.get(rel, []), key=lambda d: (d.start_byte, -d.end_byte)
        )
        lines.append(f"decls: {len(decls)}")
        for decl in decls:
            lines.append(
                f"{decl.qualified_name} | {decl.kind} | "
                f"L{decl.line_start}-L{decl.line_end} | "
                f"bytes={decl.start_byte}-{decl.end_byte} | sig={decl.signature}"
            )
        calls = sorted(
            by_file_calls.get(rel, []), key=lambda c: (c.start_byte, c.call_order)
        )
        lines.append(f"calls: {len(calls)}")
        for call in calls:
            lines.append(
                f"{call.raw_callee} -> {call.callee} | {call.confidence} | "
                f"caller={call.caller} | L{call.line_start}-L{call.line_end}"
            )
        (out_dir / f"{rel.replace('/', '__')}.parsing.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        groups = ast_grouping.build_groups(decls, data, rel)
        by_qual = {d.qualified_name: d for d in decls}
        split = ast_source_chunk.build_source_chunks(
            groups, by_qual, data, rel, repo=repo
        )
        chunk_lines = [
            f"# File: {rel}",
            (f"groups: {len(groups)} chunks: {len(split.chunks)} "
             f"empty_dropped={split.empty_dropped}"),
            "",
        ]
        for chunk in split.chunks:
            chunk_lines.append(
                f"{chunk.chunk_id} | group={chunk.group_id} | kind={chunk.span_kind} | "
                f"qual={chunk.qual} | bytes={chunk.start_byte}-{chunk.end_byte} | "
                f"L{_line_of(data, chunk.start_byte)}-L{_line_of(data, chunk.end_byte)} | "
                f"merged={chunk.merged_quals}"
            )
        (out_dir / f"{rel.replace('/', '__')}.chunks.txt").write_text(
            "\n".join(chunk_lines) + "\n", encoding="utf-8"
        )
        summary.append(f"{rel} decls={len(decls)} calls={len(calls)} "
                       f"chunks={len(split.chunks)}")
    (out_dir / "SUMMARY.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    args = _parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    langs = args.lang or list(manifest)
    for lang in langs:
        entry = manifest[lang]
        if entry.get("kind") == "network":
            root = ROOT / entry["local"]
            sample = None
        else:
            sampling = json.loads(
                (ROOT / "tests" / "fixtures" / "parsing" / "sampling.json")
                .read_text(encoding="utf-8")
            )
            root = ROOT / sampling["root"]
            sample = sampling["sample"]
        repo = f"custom/e2e-{lang}/{entry.get('tag', 'sample')}"
        for line in export_repo(lang, root, repo, args.commit, sample):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
