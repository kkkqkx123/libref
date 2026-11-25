"""Materialize grounded document-feature questions from the frozen archive.

The builder decompresses each archived page corpus into a temporary directory,
matches its raw body text with the replayed document rows, and emits questions
whose query, heading, entity, feature-node and verdict labels all have evidence
in that fixture. Existing real-site judgments are replaced, not merged.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(HERE.parents[2]))

BENCH = HERE / "bench" / "bench.json"
FIXTURES = HERE / "fixtures" / "doc_features"
QUESTIONS = FIXTURES / "questions.json"
ARCHIVE_DIR = HERE / "bench" / "fixtures"
DB_PATH = HERE / "bench" / "data" / "bench.db"
TARGET_PER_CORPUS = 30

GENERIC = {
    "api",
    "docs",
    "guide",
    "index",
    "page",
    "reference",
    "using",
    "with",
    "from",
    "this",
    "that",
    "type",
    "value",
    "client",
    "response",
    "request",
    "object",
    "name",
    "data",
    "error",
    "method",
    "string",
    "number",
    "options",
    "config",
    "version",
    "module",
    "package",
    "namespace",
    "attribute",
    "function",
    "class",
    "schemas",
    "configured",
    "documentation",
    "arguments",
    "compiled",
    "includes",
    "starts",
    "extension",
    "extensions",
    "operations",
    "properties",
    "attributes",
    "annotations",
    "configuration",
    "dependency",
    "dependencies",
    "available",
    "solution",
    "example",
    "examples",
    "description",
    "output",
    "methods",
    "parsing",
    "document",
    "documents",
    "access",
    "return",
    "returns",
    "defaults",
    "otherwise",
    "level",
    "levels",
    "logging",
    "support",
    "supported",
    "into",
    "types",
    "field",
    "fields",
    "values",
    "objects",
    "names",
    "errors",
    "input",
    "source",
    "current",
    "common",
    "custom",
    "default",
    "parameters",
    "parameter",
    "argument",
    "should",
    "need",
    "will",
    "without",
    "original",
    "service",
    "collection",
}
PREFIXES = {
    "httpx/docs": "HT",
    "zod/docs": "ZD",
    "jsoup/docs": "JS",
    "fiber/docs": "FB",
    "vue/docs": "VU",
    "pydantic/docs": "PY",
    "FluentValidation/docs": "FV",
    "Rocket/docs": "RK",
    "gradle/docs": "GR",
}
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
NAV_MARKERS = (
    "initializing search",
    "on this page",
    "table of contents",
    "copy markdown",
    "edit this page",
    "encode/httpx",
    "guide guides",
    "zod zod mini zod core blog",
    "guides async support",
    "which is implicit when loading from a url",
    "installation creating your first",
    "creating your first validator",
    "complex properties collections",
    "testing configuration deploying",
    "wrapping validators collections",
    "inclusivebetween validator",
    "making https requests to a local server proxies timeouts",
    "automatic validation (using the asp.net",
    "development proxy setup contributing thank you",
    "third party packages",
    "name stars description",
    "installation httpx a next-generation",
)
DRIFT_WORDS = (
    "deprecated",
    "deprecation",
    "removed",
    "no longer",
    "弃用",
    "已移除",
    "不再支持",
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _version_id(conn: sqlite3.Connection, info: dict[str, Any]) -> int:
    row = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id=v.library_id"
        " WHERE l.name=? AND l.ecosystem=? AND v.version=?",
        (info["library"], info["ecosystem"], info["version"]),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"missing docs version: {info['project']}")
    return int(row[0])


def _extract_archives(root: Path, temp_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in sorted(root.glob("*/docs/pages.jsonl.gz")):
        project = path.relative_to(root).parts[0]
        target = temp_dir / f"{project}.jsonl"
        with gzip.open(path, "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        out[project] = target
    return out


def _html_to_text(body: str) -> str:
    text = re.sub(
        r"<(script|style|head)[^>]*>.*?</\1>",
        " ",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"</?(h[1-6]|p|li|tr|td|th|div|section|pre|ul|ol|table)[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text)


def _read_pages(path: Path) -> dict[str, str]:
    pages: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            body = str(row.get("body") or "")
            if "<" in body[:500] and "</" in body:
                body = _html_to_text(body)
            pages[str(row["url"])] = body[:30000]
    return pages


def _strip_anchor(text: str) -> str:
    return re.sub(r"\s*\{#[^}]+\}\s*$", "", text).strip()


def _nav_heavy(text: str) -> bool:
    head = text[:200]
    titles = re.findall(r"\b[A-Z][A-Za-z-]{2,}\b", head)
    verbs = re.search(
        r"\b(is|are|can|allows|provides|def|func|class|returns)\b",
        head,
        re.IGNORECASE,
    )
    return len(titles) >= 7 and verbs is None


def _section(body: str, heading: str) -> str:
    wanted = _strip_anchor(heading).lower()
    lines = body[:30000].splitlines()
    candidates: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        match = HEADING_RE.match(stripped)
        if match is not None:
            title = _strip_anchor(match.group(2)).lower()
            if title == wanted or wanted in title:
                candidates.append((index + 1, len(match.group(1))))
        elif stripped and len(stripped) < 120:
            title = _strip_anchor(stripped).lower()
            if title == wanted or wanted in title:
                candidates.append((index + 1, 7))
    for start, level in candidates:
        collected: list[str] = []
        for line in lines[start : start + 40]:
            match = HEADING_RE.match(line.strip())
            if match is not None and len(match.group(1)) <= level:
                break
            stripped = line.strip()
            if stripped.startswith(("---", "url:")):
                continue
            if stripped.startswith(("```", "|")):
                continue
            if stripped:
                collected.append(stripped)
            if sum(len(part) for part in collected) > 3000:
                break
        section = " ".join(collected)
        head = section[:400].lower()
        if (
            len(section) < 10
            or any(marker in head for marker in NAV_MARKERS)
            or _nav_heavy(section)
        ):
            continue
        return section
    return ""


def _query(
    heading: str,
    section: str,
    aliases: list[str],
    prefer_words: tuple[str, ...] = (),
) -> str:
    clean = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", section)
    clean = re.sub(r"[#*`>_]", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    sentences = re.split(r"(?<=[.!?。！？])\s+", clean)
    chosen = sentences[0] if sentences else clean
    if prefer_words:
        for sentence in sentences:
            if any(word in sentence.lower() for word in prefer_words):
                chosen = sentence
                break
    for sentence in sentences:
        if len(sentence) >= 24 and any(
            _alias_present(alias, sentence) for alias in aliases
        ):
            chosen = sentence
            break
    if CJK_RE.search(chosen):
        excerpt = chosen[:80]
    else:
        words = chosen.split()
        excerpt = " ".join(words[:24])
    label = _strip_anchor(heading)
    return f"{label}: {excerpt}".strip()[:180]


@lru_cache(maxsize=8192)
def _aliases(name: str) -> tuple[str, ...]:
    parts = [part for part in re.split(r"[._:-]", name) if part]
    values = [name]
    if len(parts) >= 2:
        values.append(".".join(parts[-2:]))
    if parts and len(parts[-1]) >= 7 and not parts[-1].isupper():
        values.append(parts[-1])
    return tuple(
        sorted({value for value in values if len(value) >= 5}, key=len, reverse=True)
    )


@lru_cache(maxsize=32768)
def _alias_present(alias: str, text: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(alias)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _select_entities(names: list[str], text: str, focus: str) -> list[str]:
    selected: list[str] = []
    for name in names:
        for alias in _aliases(name):
            tail = alias.split(".")[-1].lower()
            if tail in GENERIC:
                continue
            haystack = text if "." in alias else focus
            if _alias_present(alias, haystack):
                selected.append(name)
                break
    return sorted(set(selected), key=len, reverse=True)[:2]


def _batches(values: list[int], size: int = 800) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _evidence(
    conn: sqlite3.Connection, version_id: int, doc_ids: list[int]
) -> tuple[
    dict[int, list[str]],
    dict[int, dict[str, dict[str, Any]]],
    dict[int, set[str]],
    dict[int, dict[str, set[str]]],
]:
    links: dict[int, list[str]] = defaultdict(list)
    facets: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    verdicts: dict[int, set[str]] = defaultdict(set)
    nodes: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for batch in _batches(doc_ids):
        marks = ",".join("?" for _ in batch)
        params = [version_id, *batch]
        for doc_id, name in conn.execute(
            "SELECT l.document_id, ae.qualified_name FROM doc_api_links l"
            " JOIN api_entities ae ON ae.id=l.api_entity_id"
            f" WHERE l.version_id=? AND l.document_id IN ({marks})",
            params,
        ).fetchall():
            links[int(doc_id)].append(str(name))
        for doc_id, facet, raw in conn.execute(
            "SELECT document_id, facet, value FROM doc_features"
            f" WHERE version_id=? AND document_id IN ({marks})",
            params,
        ).fetchall():
            try:
                value = json.loads(raw) if raw else {}
            except TypeError, ValueError:
                value = {}
            facets[int(doc_id)][str(facet)] = value if isinstance(value, dict) else {}
        for doc_id, verdict in conn.execute(
            "SELECT document_id, verdict FROM doc_consistency"
            f" WHERE version_id=? AND document_id IN ({marks})",
            params,
        ).fetchall():
            verdicts[int(doc_id)].add(str(verdict))
        for doc_id, name, path in conn.execute(
            "SELECT n.document_id, ae.qualified_name, n.node_path"
            " FROM doc_feature_node_links n"
            " JOIN api_entities ae ON ae.id=n.api_entity_id"
            f" WHERE n.version_id=? AND n.document_id IN ({marks})",
            params,
        ).fetchall():
            nodes[int(doc_id)][str(name)].add(str(path))
    return links, facets, verdicts, nodes


def _kind(value: dict[str, Any] | None) -> str:
    if value is None:
        return ""
    return str(value.get("details", {}).get("value") or "")


FACET_KEYS = (
    "block_type",
    "heading_depth",
    "length_bucket",
    "code_ratio",
    "has_table",
    "version_marker",
    "example_language",
    "example_symbols",
    "param_names",
    "return_hint",
    "deprecated",
)


def _facet_labels(doc_facets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    labels: dict[str, Any] = {}
    for key in FACET_KEYS:
        facet = doc_facets.get(key)
        if facet is None:
            continue
        value = facet.get("details", {}).get("value")
        if value is None or value == "" or value == []:
            continue
        if key == "deprecated" and not value:
            continue
        labels[key] = value
    return labels


def _question_type(
    block: str,
    deprecated: bool,
    entities: list[str],
    verdict: str,
    labels: dict[str, Any],
    drift_word: bool = False,
) -> str:
    if deprecated and verdict == "drift" and drift_word:
        return "drift"
    if block == "example":
        return "example"
    if "param_names" in labels or "return_hint" in labels:
        return "parameter"
    if "version_marker" in labels or block == "changelog":
        return "changelog"
    if labels.get("has_table"):
        return "table"
    if verdict == "drift" and drift_word:
        return "drift"
    if not entities and block == "narrative":
        return "no_answer"
    return "qualified" if entities else "semantic"


def _candidate(
    corpus: str,
    version_id: int,
    page: dict[str, Any],
    body: str,
    links: dict[int, list[str]],
    facets: dict[int, dict[str, dict[str, Any]]],
    verdicts: dict[int, set[str]],
    nodes: dict[int, dict[str, set[str]]],
    known_drift_fp: list[tuple[str, str]],
) -> dict[str, Any] | None:
    del version_id
    url = str(page["url"])
    path = str(page["path"] or "")
    heading = path.split(" / ")[-1].strip()
    if not heading or any(marker in heading.lower() for marker in NAV_MARKERS):
        return None
    if any(
        known_url == url and heading.lower() in str(known_heading).lower()
        for known_url, known_heading in known_drift_fp
    ):
        return None
    section = _section(body, heading)
    if len(section) < 24:
        return None
    doc_ids = [int(page["id"])]
    entities = _select_entities(
        links.get(doc_ids[0], []), section, f"{heading} {section[:300]}"
    )
    aliases = [alias for entity in entities for alias in _aliases(entity)]
    drift_word = any(word in section.lower() for word in DRIFT_WORDS)
    preferred = DRIFT_WORDS if drift_word else ()
    query = _query(heading, section, aliases, preferred)
    anchor_text = f"{heading} {query}"
    entities = [
        entity
        for entity in entities
        if any(_alias_present(alias, anchor_text) for alias in _aliases(entity))
    ]
    if entities:
        aliases = [alias for entity in entities for alias in _aliases(entity)]
        query = _query(heading, section, aliases, preferred)
    if len(query) < 20 or any(marker in query.lower() for marker in NAV_MARKERS):
        return None
    linked_nodes = sorted(
        {
            path
            for doc_id in doc_ids
            for entity in entities
            for path in nodes.get(doc_id, {}).get(entity, set())
        }
    )
    verdict_set = set().union(*(verdicts.get(doc_id, set()) for doc_id in doc_ids))
    verdict = verdict_set.pop() if len(verdict_set) == 1 else ""
    doc_facets = facets.get(doc_ids[0], {})
    block = _kind(doc_facets.get("block_type"))
    deprecated = bool(doc_facets.get("deprecated", {}).get("details", {}).get("value"))
    labels = _facet_labels(doc_facets)
    expected_facets: dict[str, Any] = {"heading": heading, **labels}
    digest = hashlib.sha1(f"{corpus}\x00{url}\x00{path}".encode()).hexdigest()[:12]
    item: dict[str, Any] = {
        "source": "docs",
        "source_ref": f"archive:{digest}",
        "type": _question_type(
            block,
            deprecated,
            entities,
            verdict,
            labels,
            drift_word,
        ),
        "query_lang": "zh" if CJK_RE.search(query) else "en",
        "query": query,
        "targets": [url],
        "expected_entities": entities,
        "expected_facets": expected_facets,
        "expected_verdict": verdict,
        "grades": {"strong": 2, "general": 1, "miss": 0},
        "_path": path,
        "_score": int(bool(entities)) * 3 + int(bool(block)) + int(bool(verdict)),
        "_url": url,
    }
    if entities and linked_nodes:
        item["expected_feature_nodes"] = linked_nodes
    return item


def _select_questions(
    items: list[dict[str, Any]], corpus: str, limit: int
) -> list[dict[str, Any]]:
    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_url[item["_url"]].append(item)
    for group in by_url.values():
        group.sort(key=lambda item: (-item["_score"], item["query"]))
    groups = sorted(
        by_url.values(),
        key=lambda group: (-max(item["_score"] for item in group), group[0]["_url"]),
    )
    pointer = {id(group): 0 for group in groups}
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = [0]

    def take(predicate: Any) -> bool:
        total = sum(len(group) for group in groups)
        for offset in range(total):
            position = (cursor[0] + offset) % len(groups)
            group = groups[position]
            for index in range(pointer[id(group)], len(group)):
                item = group[index]
                if item["query"] in seen or not predicate(item):
                    continue
                pointer[id(group)] = index + 1
                cursor[0] = position + 1
                seen.add(item["query"])
                chosen.append(item)
                return True
        return False

    quotas = {
        "example": 6,
        "parameter": 3,
        "changelog": 2,
        "table": 2,
        "drift": 4,
        "no_answer": 4,
        "qualified": 9,
    }
    for kind, quota in quotas.items():
        for _ in range(quota):
            if len(chosen) >= limit:
                break
            take(lambda item, kind=kind: item["type"] == kind)
    while len(chosen) < limit:
        if not take(lambda item: True):
            break
    if len(chosen) < limit:
        raise RuntimeError(
            f"only {len(chosen)} grounded questions available for {corpus}"
        )
    urls = sorted(
        {item["_url"] for item in chosen},
        key=lambda url: hashlib.sha1(url.encode()).hexdigest(),
    )
    per_url_count: dict[str, int] = defaultdict(int)
    for item in chosen:
        per_url_count[item["_url"]] += 1
    split_by_url: dict[str, str] = {}
    dev_count = 0
    for url in urls:
        if dev_count + per_url_count[url] <= limit // 2:
            split_by_url[url] = "dev"
            dev_count += per_url_count[url]
        else:
            split_by_url[url] = "test"
    for number, item in enumerate(chosen, 1):
        item["id"] = f"{PREFIXES[corpus]}{number:02d}"
        item["split"] = split_by_url[item["_url"]]
    return chosen


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def build(data: Path) -> dict[str, list[dict[str, Any]]]:
    from libref.db import apply_migrations, open_db

    manifest = _load_json(BENCH)
    current = _load_json(QUESTIONS)
    drift_samples = _load_json(FIXTURES / "drift_samples.json").get("samples", [])
    known_drift_fp = [
        (str(row["url"]), str(row["heading"]))
        for row in drift_samples
        if row.get("human_verdict") != "drift"
    ]
    conn = open_db(data / "bench.db")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    result: dict[str, list[dict[str, Any]]] = {"demo": current["demo"]}
    try:
        with tempfile.TemporaryDirectory(prefix="libref-doc-features-") as temp:
            pages_by_project = _extract_archives(ARCHIVE_DIR, Path(temp))
            for info in manifest["fixtures"]:
                if info["kind"] != "docs":
                    continue
                project = info["project"]
                corpus = f"{project}/docs"
                if project not in pages_by_project or corpus not in PREFIXES:
                    raise RuntimeError(f"missing archive material: {corpus}")
                version_id = _version_id(conn, info)
                pages = _read_pages(pages_by_project[project])
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id,url,json_extract(metadata,'$.path') path FROM documents WHERE version_id=?",
                        (version_id,),
                    ).fetchall()
                ]
                usable = [
                    row for row in rows if str(row["url"]) in pages and row["path"]
                ]
                doc_ids = [int(row["id"]) for row in usable]
                links, facets, verdicts, nodes = _evidence(conn, version_id, doc_ids)
                candidates: list[dict[str, Any]] = []
                for row in usable:
                    item = _candidate(
                        corpus,
                        version_id,
                        row,
                        pages[str(row["url"])],
                        links,
                        facets,
                        verdicts,
                        nodes,
                        known_drift_fp,
                    )
                    if item is not None:
                        candidates.append(item)
                selected = _select_questions(candidates, corpus, TARGET_PER_CORPUS)
                result[corpus] = [_clean(item) for item in selected]
                counts: dict[str, int] = defaultdict(int)
                for item in result[corpus]:
                    counts[item["type"]] += 1
                print(f"{corpus}: {len(result[corpus])} {dict(counts)}")
    finally:
        conn.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DB_PATH.parent)
    parser.add_argument("--out", type=Path, default=QUESTIONS)
    args = parser.parse_args()
    result = build(args.data)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
