"""文档端到端 runner：抓取目标站点，全量落库并导出审查报告。

用法：LIBREF_RUN_E2E=1 uv run python tests/e2e/run_doc_index.py --out /tmp/e2e-doc-report.txt
复用现有抓取编放入口，不新增抓取逻辑。
原始抓取体经快照存储留存，净化文本由导出步骤复用净化入口重新生成，
报告与逐页审查输出落盘为可读文本，不入库。
网络失败返回码 2 标记环境不可用，不视为回归。
只做参数解析加服务调用，不写业务逻辑。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOC_MANIFEST = HERE / "fixtures" / "doc_manifest.json"
OUT_ROOT = HERE / "outputs" / "docs"
SNAPSHOT_ROOT = HERE / "snapshots"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="e2e doc scrape and review runner")
    parser.add_argument("--out", default="e2e-doc-report.txt")
    parser.add_argument("--site", action="append", default=None)
    parser.add_argument("--max-pages", type=int, default=None)
    return parser.parse_args()


def _cleaned_text(raw: str, url: str, content_type: str | None) -> str:
    from libref.crawler.extract import extract_to_markdown
    from libref.text.normalize import (
        classify_content,
        normalize_markdown as normalize_text_content,
        wrap_source_content,
    )

    kind = classify_content(content_type, url)
    if kind in ("markdown", "text"):
        return normalize_text_content(raw)
    if kind == "source":
        return wrap_source_content(raw, url)
    try:
        return extract_to_markdown(raw, url)
    except Exception:  # noqa: BLE001
        return ""


def _snapshot_index(library: str, version: str, site: str) -> dict[str, dict]:
    from libref.store.snapshots import version_key

    out_dir = OUT_ROOT / site
    cleaned_dir = out_dir / "cleaned"
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    index: dict[str, dict] = {}
    version_dir = SNAPSHOT_ROOT / version_key("custom", library, version)
    if not version_dir.is_dir():
        return index
    for target in sorted(version_dir.glob("*.gz")):
        digest = target.stem
        meta = version_dir / f"{digest}.meta"
        if not meta.is_file():
            continue
        parts = meta.read_text(encoding="utf-8").split("\n")
        url = parts[0] if parts else ""
        fetched_at = parts[1] if len(parts) > 1 else ""
        content_type = parts[2] if len(parts) > 2 else ""
        try:
            raw = gzip.decompress(target.read_bytes()).decode("utf-8", "replace")
        except OSError:
            continue
        cleaned = _cleaned_text(raw, url, content_type or None)
        cleaned_path = cleaned_dir / f"{digest}.md"
        cleaned_path.write_text(cleaned, encoding="utf-8")
        index[url] = {
            "raw": str(target),
            "cleaned": str(cleaned_path),
            "raw_bytes": len(raw.encode("utf-8")),
            "cleaned_bytes": len(cleaned.encode("utf-8")),
            "fetched_at": fetched_at,
        }
    return index


def export_site(conn, library: str, version: str, site: str) -> list[str]:
    """按入库数据导出逐页审查报告，附快照索引，返回汇总行。"""
    out_dir = OUT_ROOT / site
    out_dir.mkdir(parents=True, exist_ok=True)
    version_row = conn.execute(
        "SELECT id FROM versions WHERE version=?"
        " AND library_id=(SELECT id FROM libraries WHERE name=?)",
        (version, library),
    ).fetchone()
    if version_row is None:
        return [f"{site} version missing"]
    version_id = version_row[0]
    snapshots = _snapshot_index(library, version, site)
    pages = conn.execute(
        "SELECT id, url FROM pages WHERE version_id=? ORDER BY url",
        (version_id,),
    ).fetchall()
    summary = [f"site={site} pages={len(pages)} snapshots={len(snapshots)}"]
    for url, snap in sorted(snapshots.items()):
        summary.append(
            f"snapshot {url} raw_bytes={snap['raw_bytes']} "
            f"cleaned_bytes={snap['cleaned_bytes']} fetched_at={snap['fetched_at']} "
            f"raw={snap['raw']} cleaned={snap['cleaned']}"
        )
    for page_id, url in pages:
        rows = conn.execute(
            "SELECT url, content, metadata, sort_order FROM documents"
            " WHERE page_id=? ORDER BY sort_order",
            (page_id,),
        ).fetchall()
        lines = [f"# Page: {url}", f"chunks: {len(rows)}"]
        snap = snapshots.get(url)
        if snap is not None:
            lines.append(f"snapshot raw={snap['raw']} cleaned={snap['cleaned']}")
        lines.append("")
        for doc_url, content, metadata, sort_order in rows:
            try:
                meta = json.loads(metadata) if metadata else {}
            except ValueError:
                meta = {}
            text = content or ""
            lines.append(
                f"order={sort_order} | path={meta.get('chunk_path', '')} | "
                f"kind={meta.get('span_kind', '')} | "
                f"title={meta.get('title', '')} | "
                f"chars={len(text)} | head={text[:120].replace(chr(10), ' ')}"
            )
        safe = "".join(c if c.isalnum() else "_" for c in url)[:80]
        (out_dir / f"{safe}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary.append(f"{url} chunks={len(rows)}")
    (out_dir / "SUMMARY.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    args = _parse_args()
    from libref.config import LibrefSettings
    from libref.db import apply_migrations, open_db
    from libref.services.ingest import IngestService

    manifest = json.loads(DOC_MANIFEST.read_text(encoding="utf-8"))
    sites = args.site or list(manifest)
    lines: list[str] = [f"manifest={DOC_MANIFEST}"]
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_db(Path(tmp) / "e2e-doc.db")
        apply_migrations(conn)
        try:
            settings = LibrefSettings(
                crawl_delay_default=0.0,
                snapshot_enabled=True,
                snapshot_dir=SNAPSHOT_ROOT,
                snapshot_quota_mb=64,
            )
            service = IngestService(settings, conn)
            for site in sites:
                entry = manifest[site]
                library = f"e2e-docs-{site}"
                started = time.time()
                try:
                    counts = service.scrape_docs(
                        library, entry["version"], entry["url"],
                        max_pages=args.max_pages or entry.get("max_pages"),
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"SKIP env unavailable for {site}: {exc}", file=sys.stderr)
                    return 2
                elapsed = time.time() - started
                lines.append(
                    f"{site} version={entry['version']} counts={counts} "
                    f"elapsed={elapsed:.1f}s"
                )
                lines.extend(export_site(conn, library, entry["version"], site))
        finally:
            conn.close()
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
