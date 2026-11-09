# AGENTS.md — libref Development Conventions

## Guideline

- **No backward compatibility.** The project is in active development. Prioritize
  long-term architecture maintainability and fix design defects early. Never keep
  legacy code or shim old data.
- **Document Reference Rule.** Code comments describe intent only. Never cite plan
  or doc structure identifiers (e.g. P1, P2-3, T1, section numbers, phase names)
  in code comments.
- **Plan/Design Documents.** Use concise natural language. Avoid complete code snippets.

## Language

- **Code files** (including config files, identifiers, and all comments/docstrings):
  English only.
- **Documentation** (`docs/`, in-doc explanations): Simplified Chinese only.
- A module docstring may cite a blueprint name (e.g. `ref/PrefixSpan-py`), which
  stays in its original form; the surrounding explanation is still English.

## Project Overview

`libref` is a library-reference knowledge base: it crawls library documentation,
mines open-source usage of the same libraries, and serves grounded answers
through CLI / MCP / Web exits.

- **Branch A — doc crawl** (`crawler/` → `chunking/`): robots/sitemap/`llms.txt`
  discovery → httpx fetch with incremental revalidation → main-content extraction
  → heading-path structural chunking → SQLite + FTS5.
- **Branch B — source mining** (`astminer/` → `mining/`): clone repos (dulwich) →
  tree-sitter AST extraction of classes/functions/imports/call instances → two-pass
  cross-file alias resolution → frequency / co-occurrence / PrefixSpan sequence
  mining → `seq_patterns`.
- **Retrieval** (`store/` + `llm/`): FTS5 BM25 + Qdrant KNN → RRF → frequency
  rerank → grounded answer generation. SQLite is the sole source of truth;
  Qdrant vectors are a rebuildable derived index owned only by `store/vec_store.py`.
- **Orchestration** (`services/`): the only layer exits may call.

Details: `docs/architecture/tech-stack.md`, `docs/architecture/module-layout.md`,
`docs/architecture/code-indexing-chain.md`, `docs/architecture/doc-indexing-chain.md`.

## Environment (uv Only)

All Python environment operations go through `uv`. Never use system pip or
global installs.

```bash
uv sync                    # create/sync .venv from uv.lock (includes dev group)
uv add <pkg>               # add a runtime dependency (updates pyproject + uv.lock)
uv add --optional browser <pkg>  # optional extra (e.g. playwright)
uv run <cmd>               # run anything inside .venv (pytest, python, ruff, mypy)
```

- The venv lives at project root `.venv/` (created by `uv sync`). Never activate
  it manually and never call `.venv/bin/python` directly — always use `uv run`.
- Dependency changes only via `uv add` / `uv remove`, or by editing
  `pyproject.toml` followed by `uv sync`. `pip install` is forbidden (it desyncs
  `uv.lock`).
- Python is pinned by `.python-version` (3.14). A version bump must re-verify
  binary dependencies (tree-sitter grammar wheels, sqlite-vec, …).

## Workspace Layout

Source in `src/libref/`, tests in `tests/`. Full tree: `docs/architecture/module-layout.md`.

```
crawler ─┐
chunking ├→ db ←─ store ←─ services ←─ {cli, mcp_server, web}
astminer ┘   ↑       ↑
mining ──────┘       llm (transport leaf over httpx; consumed via injection/constructors)
              └→ Qdrant (reachable only from store/vec_store.py)
```

- **One-way dependencies.** `db` is the only shared SQL bottom layer; `services/`
  is the only orchestration layer. The three exits (`cli`, `mcp_server`, `web`)
  must never bypass `services/` to call `store`/`crawler`/`mining` directly.
- **Thin exits.** Exit modules do argument parsing + `services/` calls + result
  formatting only. No business logic.
- **Pure algorithms.** `mining/` and AST extraction cores are pure in-memory code
  with zero `db` dependency (plain-Python inputs); persistence is orchestrated
  by `services/`.
- **Branch independence.** `astminer/` and `crawler/` do not depend on each other;
  they merge only through `db`, orchestrated by `services/`.
- **Pydantic at boundaries.** Cross-package schemas (extraction records, call
  instances, MCP parameters, REST responses) are pydantic models derived from one
  definition. No field drift.
- **`ref/` is read-only.** External reference checkouts never participate in the
  build and never become dependencies.

## Command Execution

```bash
uv run pytest              # full test suite (minimum check before submit)
uv run ruff check src tests  # lint
uv run ruff format --check src tests  # format check
uv run mypy src            # type check
```

Consolidate verification into as few runs as possible. When filtering long build
output repeatedly, redirect to a file first, then read it.

## Testing

- **Organization.** Unit tests live next to the code (`test_*.py` mirroring the
  module); integration tests under `tests/integration/`; network-dependent E2E
  under `tests/e2e/`; parse samples under `tests/fixtures/` (fixtures may
  intentionally contain unresolved calls/idle bindings — see ruff
  per-file-ignores in `pyproject.toml`).
- **Markers** (`pyproject.toml`): `unit` (pure in-memory, no DB/subprocess),
  `integration` (needs sqlite/subprocess/external service), `e2e` (needs real
  network, skipped unless `LIBREF_RUN_E2E=1`), `requires_qdrant` (needs local
  Qdrant), `requires_binary` (needs built entrypoint).
- **Golden samples.** Every borrowed algorithm keeps golden cases with annotated
  provenance (upstream README/paper). Retrieval quality baselines (MRR/Recall@k)
  are CI gates — never silently loosen thresholds.
- Changes touching retrieval or mining logic must come with golden or
  differential (cross-check) tests.

## Workflow

### Doc pipeline

```
Discover (robots → sitemap/llms.txt) → Fetch (etag/last-modified, per-host backoff)
→ Extract (main-content prune → markdown) → Chunk (heading path + greedy merge,
fence-balance invariant) → SQLite + FTS → services.ingest orchestration
```

### Source-mining pipeline

```
Clone/update repos → Parse (tree-sitter, per-language queries) → Group
→ Extract call instances (line range + args + confidence) → Resolve (declare/import
facts, then cross-file aliases) → Aggregate (frequency/co-occurrence/PrefixSpan closed)
→ services.mining_refresh persists to seq_patterns
```

### Retrieval pipeline

```
Query → FTS5 BM25 + Qdrant KNN → RRF fusion → frequency rerank → passage assembly
→ grounded answer (llm gateway: shared resilience state per endpoint)
```

## Code Conventions

- **Blueprint attribution.** Each module borrowed from an external project states
  the blueprint on the module docstring's first line
  (e.g. `Blueprint ref/PrefixSpan-py (MIT)`) plus what was customized.
- **New language support** = add a tree-sitter grammar wheel + a `LanguageConfig`
  in `astminer/config.py`. Never touch the engine.
- **Config.** `config.py` (pydantic-settings) is shared by exits and `services/`;
  `db` never depends on it. Precedence: explicit CLI overrides > environment
  (including local file injection) > config file > built-in defaults. The
  effective snapshot is frozen read-only once built.
- **Secrets** travel via environment variables only, never via config files.
  Logs print redacted snapshots only (`redacted_dict`).
- **Cross-file sync in one change.** When touching a contract with multiple
  mirrors (config defaults/docs, Qdrant collection governance, grammar
  wheel/`LanguageConfig`/fixtures), update all mirrors in the same change.
