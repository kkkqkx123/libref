"""Draft document-feature questions from the frozen retrieval judgments.

Usage: uv run python tests/e2e/generate_doc_feature_questions.py [--out FILE]
Candidates are derived only from judgments.json: no corpus, derivation service,
or model output feeds the candidate set. Drafts are written to the ignored
outputs directory and need human confirmation before entering questions.json.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
JUDGMENTS = HERE / "bench" / "judgments.json"
DEFAULT_OUT = HERE / "outputs" / "doc_features" / "question_candidates.json"
_SYMBOL_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,5}\b")
_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]{2,}\b")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="draft document feature judgments")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


def _symbols(text: str) -> set[str]:
    value = str(text or "")
    found = {match.group(0) for match in _SYMBOL_RE.finditer(value)}
    found.update(match.group(0) for match in _NAME_RE.finditer(value))
    return found


def _path_symbols(targets: list[Any]) -> set[str]:
    """Source targets are file paths; their stems name the API under discussion."""
    out: set[str] = set()
    for target in targets:
        stem = str(target).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        for part in re.split(r"[_\-]", stem):
            if len(part) >= 4 and part.isidentifier():
                out.add(part)
    return out


def _docs_by_project(judgments: dict[str, list[dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for corpus, entries in judgments.items():
        if not corpus.endswith("/docs"):
            continue
        project = corpus.removesuffix("/docs")
        for entry in entries:
            if entry.get("targets"):
                out.setdefault(project, []).append(entry)
    return out


def _draft(
    entry: dict,
    corpus: str,
    source: str,
    kind: str,
    *,
    targets: list[str],
    symbols: set[str],
    matched: list[str],
) -> dict[str, Any]:
    """One candidate: expected entity values stay empty until a human confirms them."""
    status = "ready" if targets and source in ("docs", "cross") else "draft"
    return {
        "id": f"{corpus}:{entry['id']}:{kind}",
        "status": status,
        "source": source,
        "source_ref": f"{corpus}:{entry['id']}" + (f"/{matched[0]}" if matched else ""),
        "type": str(entry.get("type") or "qualified"),
        "split": str(entry.get("split") or "dev"),
        "query_lang": str(entry.get("query_lang") or "en"),
        "query": str(entry.get("query") or ""),
        "targets": targets,
        "expected_entities": sorted(symbols)
        if status == "ready" and source == "cross"
        else [],
        "expected_facets": (
            {"heading": str(entry["heading"])} if entry.get("heading") else {}
        ),
        "expected_verdict": "",
        "grades": {"strong": 2, "general": 1, "miss": 0},
        "note": (
            "docs anchor inherited; confirm entity and block type"
            if source == "docs"
            else "source symbol and docs anchor both present; confirm entity"
            if status == "ready"
            else "one-sided evidence; not eligible for scoring"
        ),
    }


def generate(judgments: dict[str, list[dict]]) -> list[dict[str, Any]]:
    """Build candidates with the branch provenance rules, keeping drafts separate."""
    docs = _docs_by_project(judgments)
    out: list[dict[str, Any]] = []
    for corpus, entries in judgments.items():
        project = corpus.split("/", 1)[0]
        is_docs = corpus.endswith("/docs")
        for entry in entries:
            if entry.get("type") == "absent" or not entry.get("targets"):
                continue
            symbols = _symbols(entry.get("query", "")) | _path_symbols(entry["targets"])
            if is_docs:
                out.append(
                    _draft(
                        entry,
                        corpus,
                        "docs",
                        "doc",
                        targets=[str(url) for url in entry["targets"]],
                        symbols=set(),
                        matched=[],
                    )
                )
                continue
            matches: list[tuple[str, dict]] = []
            for doc_entry in docs.get(project, []):
                haystack = f"{doc_entry.get('query', '')} {' '.join(doc_entry.get('targets', []))}"
                if any(symbol in haystack for symbol in symbols):
                    matches.append((doc_entry["id"], doc_entry))
            if not matches:
                out.append(
                    _draft(
                        entry,
                        corpus,
                        "code",
                        "code",
                        targets=[],
                        symbols=symbols,
                        matched=[],
                    )
                )
                continue
            for doc_id, doc_entry in matches:
                candidate = _draft(
                    entry,
                    corpus,
                    "cross",
                    f"doc-{doc_id}",
                    targets=[str(url) for url in doc_entry["targets"]],
                    symbols=symbols,
                    matched=[doc_id],
                )
                candidate["query"] = f"which documentation example shows {min(symbols)}"
                candidate["expected_facets"] = (
                    {"heading": str(doc_entry["heading"])}
                    if doc_entry.get("heading")
                    else {}
                )
                out.append(candidate)
    return out


def main() -> int:
    args = _parse_args()
    judgments = json.loads(JUDGMENTS.read_text(encoding="utf-8"))
    candidates = generate(judgments)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ready = sum(1 for item in candidates if item["status"] == "ready")
    print(
        f"{len(candidates)} candidates ({ready} ready, {len(candidates) - ready} drafts) -> {out}"
    )
    print(
        "Confirm candidates by copying verified entries into fixtures/doc_features/questions.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
