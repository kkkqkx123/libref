"""抓取编排：发现地址、抓取、净化、分块、落库、同步向量。

编排层是唯一的调度位置，出口层只调本模块。向量是派生索引：
真源落库成功后才同步向量，向量失败只计数，不推翻真源写入。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from libref.chunking import split_markdown
from libref.crawler import (
    HttpFetcher,
    RobotsChecker,
    UrlSeeder,
    build_scope,
    extract_to_markdown,
    looks_like_js_rendered,
)
from libref.llm import EmbedFn
from libref.services.jobs import Job, JobManager
from libref.services.search import SearchService
from libref.store.document_store import DocumentStore, StoreResult
from libref.store.vec_store import QdrantVecStore, build_embed_text

_DEFAULT_SEED_HOST_TTL = 7.0
_EMBED_BATCH = 64

SCRAPE_JOB_KIND = "scrape_docs"
"""抓取任务在任务表中的类型名。"""


def scrape_job_params(
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
) -> dict[str, Any]:
    """构造抓取任务参数，含解析来源以保证可重放。"""
    return {
        "library": library,
        "version": version,
        "start_url": start_url,
        "max_pages": max_pages,
        "prefilter": prefilter,
        "scope": scope or {},
        "ecosystem": ecosystem,
        "resolved_from": resolved_from,
        "registry_confirmed": registry_confirmed,
    }


def start_scrape_job(
    mgr: JobManager,
    library: str,
    version: str,
    start_url: str,
    *,
    max_pages: int | None = None,
    prefilter: bool = False,
    scope: dict[str, Any] | None = None,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
) -> int:
    """提交抓取任务到队列，返回任务标识，执行由 worker 加 runner 完成。"""
    return mgr.submit(
        SCRAPE_JOB_KIND,
        scrape_job_params(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
        ),
    )


_version_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_version_locks_guard = threading.Lock()


def _version_lock(ecosystem: str, library: str, version: str) -> asyncio.Lock:
    """同版本同时只允许一个抓取任务执行，第二个任务排队等待。"""
    key = (ecosystem, library, version)
    with _version_locks_guard:
        lock = _version_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _version_locks[key] = lock
        return lock


def build_scrape_runner(settings, conn, *, vec_store=None, embed=None):
    """构造任务消费函数，按任务参数执行整站抓取。"""
    service = IngestService(settings, conn, vec_store=vec_store, embed=embed)

    async def runner(job: Job) -> None:
        if job.kind != SCRAPE_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        params = job.params
        await service.scrape_docs_async(
            params["library"],
            params["version"],
            params["start_url"],
            max_pages=params.get("max_pages"),
            prefilter=params.get("prefilter", False),
            scope=params.get("scope"),
            ecosystem=params.get("ecosystem", "custom"),
            resolved_from=params.get("resolved_from"),
            registry_confirmed=params.get("registry_confirmed", True),
        )

    return runner


def run_scrape_job_sync(
    mgr: JobManager,
    settings,
    conn,
    job_id: int,
    *,
    timeout: float = 300.0,
    vec_store=None,
    embed: EmbedFn | None = None,
) -> Job:
    """同步驱动单任务：起 worker、等待落定、停 worker，供命令行异步开关使用。"""

    async def _drive() -> Job:
        mgr.start_worker(
            build_scrape_runner(settings, conn, vec_store=vec_store, embed=embed)
        )
        try:
            return await mgr.wait_for(job_id, timeout=timeout)
        finally:
            mgr.stop_worker()

    return asyncio.run(_drive())


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        timeout=settings.request_timeout,
        follow_redirects=True,
        headers={"User-Agent": getattr(settings, "user_agent", "libref-crawler/0.1")},
        http2=True,
    ) as client:
        yield client


class IngestService:
    def __init__(
        self,
        settings,
        conn,
        *,
        vec_store: QdrantVecStore | None = None,
        embed: EmbedFn | None = None,
    ) -> None:
        self._settings = settings
        self._conn = conn
        self._vec = vec_store if embed is not None else None
        self._embed = embed if vec_store is not None else None

    def scrape_docs(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
        homepage: str | None = None,
        repo_url: str | None = None,
    ) -> dict[str, int]:
        """同步入口：发现、抓取、净化、分块、落库，返回各类结局计数。"""
        return asyncio.run(
            self.scrape_docs_async(
                library,
                version,
                start_url,
                max_pages=max_pages,
                prefilter=prefilter,
                scope=scope,
                ecosystem=ecosystem,
                resolved_from=resolved_from,
                registry_confirmed=registry_confirmed,
                homepage=homepage,
                repo_url=repo_url,
            )
        )

    async def scrape_docs_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
        homepage: str | None = None,
        repo_url: str | None = None,
    ) -> dict[str, int]:
        """异步入口，任务 worker 复用，避免在运行中的事件循环内嵌套启动。"""
        return await self._scrape_async(
            library,
            version,
            start_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            homepage=homepage,
            repo_url=repo_url,
        )

    def scrape_by_name(
        self,
        ecosystem: str | None,
        package: str,
        requested_version: str | None = None,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """按名称解析后抓取：先解析出地址与版本，再走标准抓取链路。"""
        return asyncio.run(
            self.scrape_by_name_async(
                ecosystem,
                package,
                requested_version,
                max_pages=max_pages,
                prefilter=prefilter,
                scope=scope,
            )
        )

    async def scrape_by_name_async(
        self,
        ecosystem: str | None,
        package: str,
        requested_version: str | None = None,
        *,
        max_pages: int | None = None,
        prefilter: bool = False,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """按名称解析后抓取的异步入口，供任务 worker 复用。"""
        from libref.services.resolver import resolve_package

        async with _client(self._settings) as client:
            resolution = await resolve_package(
                self._conn,
                client,
                self._settings,
                ecosystem,
                package,
                requested_version,
            )
        assert resolution.docs_url is not None
        return await self._scrape_async(
            resolution.name,
            resolution.version,
            resolution.docs_url,
            max_pages=max_pages,
            prefilter=prefilter,
            scope=scope,
            ecosystem=resolution.ecosystem,
            resolved_from=resolution.source,
            registry_confirmed=resolution.registry_confirmed,
            homepage=resolution.homepage,
            repo_url=resolution.repo_url,
        )

    async def _scrape_async(
        self,
        library: str,
        version: str,
        start_url: str,
        *,
        max_pages: int | None,
        prefilter: bool,
        scope: dict[str, Any] | None = None,
        ecosystem: str = "custom",
        resolved_from: str | None = None,
        registry_confirmed: bool = True,
        homepage: str | None = None,
        repo_url: str | None = None,
    ) -> dict[str, int]:
        counts = {
            k: 0
            for k in (
                "stored",
                "unchanged",
                "absent",
                "empty",
                "skipped",
                "failed",
                "vector_failed",
            )
        }
        async with _version_lock(ecosystem, library, version):
            removed_all: list[int] = []
            inserted_all: list[int] = []
            async with _client(self._settings) as client:
                robots = RobotsChecker(
                    self._conn,
                    client,
                    ttl_days=getattr(
                        self._settings, "robots_cache_ttl_days", _DEFAULT_SEED_HOST_TTL
                    ),
                    user_agent=getattr(
                        self._settings, "user_agent", "libref-crawler/0.1"
                    ),
                )
                seeder = UrlSeeder(client, robots)
                fetcher = HttpFetcher(
                    client,
                    max_retries=getattr(self._settings, "fetch_max_retries", 3),
                    default_delay=getattr(self._settings, "crawl_delay_default", 1.0),
                )
                seed = await seeder.discover(
                    start_url, prefilter=prefilter, max_pages=None
                )
                policy = build_scope(start_url, scope)
                candidates = seed.urls + [
                    u for u in seed.cross_site if u not in seed.urls
                ]
                accepted, rejected = policy.filter(candidates)
                counts["skipped"] += len(rejected)
                if seed.source == "none" and seed.failed:
                    counts["failed"] += 1
                ingest_truncated = False
                if max_pages is not None and len(accepted) > max_pages:
                    accepted = accepted[:max_pages]
                    ingest_truncated = True
                truncated = seed.truncated or ingest_truncated

                store = DocumentStore(self._conn)
                search = SearchService(self._conn)
                library_id, version_id = search.ensure_library_version(
                    library,
                    version,
                    ecosystem,
                    homepage=homepage,
                    docs_url=start_url,
                    repo_url=repo_url,
                    resolved_from=resolved_from
                    or ("explicit" if ecosystem == "custom" else None),
                    registry_confirmed=registry_confirmed,
                )

                sem = asyncio.Semaphore(getattr(self._settings, "fetch_concurrency", 4))

                async def process(url: str) -> None:
                    async with sem:
                        try:
                            page = store.get_page(version_id, url)
                            result = await self._fetch_and_store(
                                fetcher,
                                robots,
                                store,
                                library_id,
                                version_id,
                                url,
                                page,
                            )
                        except Exception:  # noqa: BLE001 —— 单页失败隔离计数，不中断整站抓取
                            result = StoreResult("failed")
                        counts[result.outcome] = counts.get(result.outcome, 0) + 1
                        removed_all.extend(result.removed_ids)
                        inserted_all.extend(result.inserted_ids)

                await asyncio.gather(*(process(u) for u in accepted))
                removed_all.extend(
                    self._sweep_absent(
                        store,
                        library_id,
                        version_id,
                        seed,
                        accepted,
                        truncated=truncated,
                        counts=counts,
                    )
                )
                await self._sync_vectors(store, counts, removed_all, inserted_all)
            return counts

    async def _sync_vectors(
        self,
        store: DocumentStore,
        counts: dict[str, int],
        removed: list[int],
        inserted: list[int],
    ) -> None:
        """收尾批量同步向量：集合校验、删旧点、编码、写入，分段计数失败。"""
        if self._vec is None or self._embed is None:
            return
        if not removed and not inserted:
            return
        assert self._vec is not None and self._embed is not None
        try:
            self._vec.ensure_collection(
                self._conn,
                getattr(self._settings, "embedding_model", ""),
                getattr(self._settings, "embedding_dim", 0),
            )
        except Exception:  # noqa: BLE001 —— 派生索引失败只计数，不推翻真源写入
            counts["vector_failed"] += len(removed) + len(inserted)
            return
        try:
            self._vec.delete_docs(removed)
        except Exception:  # noqa: BLE001 —— 派生索引失败只计数，不推翻真源写入
            counts["vector_failed"] += len(removed) + len(inserted)
            return
        try:
            docs = store.get_documents(inserted)
            texts = [
                build_embed_text(
                    d["metadata"].get("title"), d["metadata"].get("path"), d["content"]
                )
                for d in docs
            ]
            vecs = await self._embed(texts) if texts else []
            if len(vecs) != len(docs):
                raise RuntimeError("embedding count mismatch")
            self._vec.upsert_batch(
                [
                    (d["id"], d["library_id"], d["version_id"], vec)
                    for d, vec in zip(docs, vecs)
                ]
            )
        except Exception:  # noqa: BLE001 —— 派生索引失败只计数，不推翻真源写入
            counts["vector_failed"] += len(inserted)

    def _sweep_absent(
        self,
        store: DocumentStore,
        library_id: int,
        version_id: int,
        seed,
        accepted_urls: list[str],
        *,
        truncated: bool,
        counts: dict[str, int],
    ) -> list[int]:
        """缺席清理：已有页面减去本次发现集，返回删掉的文档标识。

        有效完整发现才执行；截断或失败一律跳过，避免误删。
        """
        if seed.source == "none":
            return []
        if seed.failed or truncated:
            return []
        removed: list[int] = []
        discovered = set(accepted_urls)
        for url in store.list_page_urls(version_id):
            if url not in discovered:
                result = store.store_page(version_id, library_id, url, "absent")
                removed.extend(result.removed_ids)
                counts["absent"] = counts.get("absent", 0) + 1
        return removed

    async def _fetch_and_store(
        self,
        fetcher: HttpFetcher,
        robots: RobotsChecker,
        store: DocumentStore,
        library_id: int,
        version_id: int,
        url: str,
        page,
    ) -> StoreResult:
        if not await robots.can_fetch(url):
            return StoreResult("skipped")
        delay = await robots.crawl_delay(url)
        result = await fetcher.fetch(
            url,
            etag=page.etag if page else None,
            last_modified=page.last_modified if page else None,
            crawl_delay=delay,
        )
        if result.status == "unchanged":
            return store.store_page(version_id, library_id, url, "unchanged")
        if result.status == "absent":
            return store.store_page(version_id, library_id, url, "absent")
        if result.status == "failed":
            return store.store_page(version_id, library_id, url, "failed")

        raw_html = result.content or ""
        try:
            markdown = extract_to_markdown(raw_html, url)
        except Exception:  # noqa: BLE001 —— 净化失败计空，由下游结局分支处理
            markdown = ""
        if not markdown:
            if looks_like_js_rendered(raw_html, markdown):
                return StoreResult("skipped")
            return store.store_page(
                version_id,
                library_id,
                url,
                "empty",
                raw_content=result.content,
                etag=result.etag,
                last_modified=result.last_modified,
            )
        outcome = store.ensure_changed(version_id, url, markdown)
        if outcome == "unchanged":
            return store.store_page(version_id, library_id, url, "unchanged")
        chunks = split_markdown(markdown, url)
        return store.store_page(
            version_id,
            library_id,
            url,
            "stored",
            chunks=chunks,
            raw_content=markdown,
            etag=result.etag,
            last_modified=result.last_modified,
        )


INDEX_REPO_JOB_KIND = "index_repo"
MINE_PATTERNS_JOB_KIND = "mine_patterns"


def entity_uid(version_id: int, qualified_name: str, kind: str) -> str:
    import hashlib

    blob = f"{version_id}\0{qualified_name}\0{kind}".encode()
    return hashlib.sha1(blob).hexdigest()


def call_uid(
    repo: str,
    caller_file: str,
    line_start: int,
    line_end: int,
    callee: str,
    args: list[str],
) -> str:
    import hashlib

    blob = "\0".join(
        [repo, caller_file, str(line_start), str(line_end), callee, "\x1f".join(args)]
    ).encode()
    return hashlib.sha1(blob).hexdigest()


def _collect_repo(
    conn,
    repo_name: str,
    root,
    files: list[str],
) -> tuple[
    list,
    dict[str, list[tuple[str, str]]],
    list,
    dict[str, int],
    dict[str, tuple[str, int]],
]:
    import hashlib
    from pathlib import Path

    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import engine as ast_engine

    all_decls: list = []
    file_aliases: dict[str, list[tuple[str, str]]] = {}
    all_raw: list = []
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
            decls, aliases, raw_calls = ast_cache.decode_records(hit[1], rel)
            all_decls.extend(decls)
            file_aliases[rel] = aliases
            all_raw.extend(raw_calls)
            continue
        counts["parsed"] += 1
        decls, aliases, raw_calls = ast_engine.extract_file(rel, cfg, data)
        all_decls.extend(decls)
        file_aliases[rel] = list(aliases)
        all_raw.extend(raw_calls)
        ast_cache.store(
            conn,
            repo_name,
            rel,
            sha,
            fp,
            cfg.config_version,
            ast_cache.encode_records(decls, aliases, raw_calls),
        )
    return all_decls, file_aliases, all_raw, counts, live


def _persist_entities(
    conn, library_id: int, version_id: int, all_decls
) -> tuple[int, int]:
    import json as _decl_json

    pending: dict[tuple[str, str], object] = {}
    for decl in all_decls:
        pending[(decl.qualified_name, decl.kind)] = decl
    for qualified_name, kind in list(pending):
        decl = pending[(qualified_name, kind)]
        uid = entity_uid(version_id, qualified_name, kind)
        conn.execute(
            "INSERT INTO api_entities(entity_uid, library_id, version_id,"
            " qualified_name, kind, parent, signature, params, bases,"
            " start_byte, end_byte, depth) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(entity_uid) DO UPDATE SET parent=excluded.parent,"
            " signature=excluded.signature, params=excluded.params, bases=excluded.bases,"
            " start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            " depth=excluded.depth",
            (
                uid,
                library_id,
                version_id,
                qualified_name,
                kind,
                decl.parent,
                decl.signature,
                _decl_json.dumps(list(decl.params), ensure_ascii=False),
                _decl_json.dumps(list(decl.bases), ensure_ascii=False),
                decl.start_byte,
                decl.end_byte,
                decl.depth,
            ),
        )
    wanted = {entity_uid(version_id, q, k) for q, k in pending}
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        dead = conn.execute(
            f"SELECT entity_uid FROM api_entities WHERE version_id=?"
            f" AND entity_uid NOT IN ({placeholders})",
            (version_id, *sorted(wanted)),
        ).fetchall()
        if dead:
            conn.execute(
                f"DELETE FROM api_entities WHERE version_id=?"
                f" AND entity_uid NOT IN ({placeholders})",
                (version_id, *sorted(wanted)),
            )
    else:
        dead = conn.execute(
            "SELECT entity_uid FROM api_entities WHERE version_id=?", (version_id,)
        ).fetchall()
        if dead:
            conn.execute("DELETE FROM api_entities WHERE version_id=?", (version_id,))
    return len(pending), len(dead)


def _persist_calls(
    conn, library_id: int, version_id: int, resolved, commit_sha: str = ""
) -> tuple[int, int]:
    import json as _uid_json

    entity_ids = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT qualified_name || '|' || kind, id FROM api_entities"
            " WHERE version_id=?",
            (version_id,),
        )
    }
    kinds = ("function", "method", "class", "interface", "enum", "constructor")
    wanted: set[str] = set()
    for call in resolved:
        args_json = _uid_json.dumps(list(call.args), ensure_ascii=False)
        uid = call_uid(
            call.repo,
            call.caller_file,
            call.line_start,
            call.line_end,
            call.callee,
            list(call.args),
        )
        wanted.add(uid)
        ent_id = None
        for kind in kinds:
            ent_id = entity_ids.get(f"{call.callee}|{kind}")
            if ent_id is not None:
                break
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_entity_id, callee_name, args,"
            " repo, caller_file, line_start, line_end, confidence,"
            " library_id, version_id, caller, receiver, func, argument_count,"
            " call_order, start_byte, end_byte, commit_sha)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(call_uid) DO UPDATE SET callee_entity_id=excluded.callee_entity_id,"
            " callee_name=excluded.callee_name, args=excluded.args,"
            " confidence=excluded.confidence, library_id=excluded.library_id,"
            " version_id=excluded.version_id, caller=excluded.caller,"
            " receiver=excluded.receiver, func=excluded.func,"
            " argument_count=excluded.argument_count, call_order=excluded.call_order,"
            " start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            " commit_sha=excluded.commit_sha",
            (
                uid,
                ent_id,
                call.callee,
                args_json,
                call.repo,
                call.caller_file,
                call.line_start,
                call.line_end,
                call.confidence,
                library_id,
                version_id,
                call.caller,
                call.receiver,
                call.func,
                call.argument_count,
                call.call_order,
                call.start_byte,
                call.end_byte,
                commit_sha or call.commit_sha,
            ),
        )
        if call.confidence == "AMBIGUOUS":
            row = conn.execute(
                "SELECT id FROM call_instances WHERE call_uid=?", (uid,)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT INTO review_status(entity_type, entity_id, status)"
                    " VALUES('call_instance', ?, 'pending')"
                    " ON CONFLICT(entity_type, entity_id) DO NOTHING",
                    (int(row[0]),),
                )
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        dead_rows = conn.execute(
            f"SELECT id, call_uid FROM call_instances WHERE version_id=?"
            f" AND call_uid NOT IN ({placeholders})",
            (version_id, *sorted(wanted)),
        ).fetchall()
    else:
        dead_rows = conn.execute(
            "SELECT id, call_uid FROM call_instances WHERE version_id=?", (version_id,)
        ).fetchall()
    for dead_id, _ in dead_rows:
        conn.execute(
            "DELETE FROM review_status WHERE entity_type='call_instance' AND entity_id=?",
            (int(dead_id),),
        )
    if dead_rows:
        dead_uids = sorted(uid for _, uid in dead_rows)
        placeholders = ",".join("?" for _ in dead_uids)
        conn.execute(
            f"DELETE FROM call_instances WHERE version_id=?"
            f" AND call_uid IN ({placeholders})",
            (version_id, *dead_uids),
        )
    return len(resolved), len(dead_rows)


def index_repo(
    conn,
    library: str,
    version: str,
    source: str,
    *,
    update: bool = False,
    repos_dir=None,
    ecosystem: str = "custom",
) -> dict[str, int]:
    from pathlib import Path

    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import resolution as ast_resolution
    from libref.astminer.repos import RepoManager, iter_source_files
    from libref.services.search import SearchService

    settings_dir = Path(repos_dir) if repos_dir is not None else Path("data/repos")
    search = SearchService(conn)
    library_id, version_id = search.ensure_library_version(
        library,
        version,
        ecosystem,
        repo_url=source,
        resolved_from="explicit",
        registry_confirmed=True,
    )
    manager = RepoManager(settings_dir)
    repo_name = f"{ecosystem}/{library}/{version}"
    existing = conn.execute(
        "SELECT url, local_path FROM repos WHERE name=?", (repo_name,)
    ).fetchone()
    if existing is not None:
        old_url, old_path = existing[0], existing[1]
        if old_url != source and str(source) != str(old_path):
            raise ValueError(
                f"version already indexed from different source: {old_url}"
            )
    root, commit = manager.ensure(conn, repo_name, source, update=update)
    conn.commit()
    files = [r for r in iter_source_files(root, ast_config.all_suffixes())]
    all_decls, file_aliases, all_raw, counts, live = _collect_repo(
        conn,
        repo_name,
        root,
        files,
    )
    counts["evicted"] = ast_cache.sweep_repo(conn, repo_name, live)
    conn.commit()
    resolved = ast_resolution.resolve_calls(
        all_decls, file_aliases, all_raw, repo_name, commit
    )
    n_entities, swept_entities = _persist_entities(
        conn, library_id, version_id, all_decls
    )
    n_calls, swept_calls = _persist_calls(
        conn, library_id, version_id, resolved, commit
    )
    conn.commit()
    counts["entities"] = n_entities
    counts["calls"] = n_calls
    counts["swept_entities"] = swept_entities
    counts["swept_calls"] = swept_calls
    return counts


def _clear_derived(conn, version_id: int) -> None:
    """清空版本内全部派生挖掘产出，调用为空或序列为空时保持三表一致。"""
    conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
    conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))


def refresh_feature_tree(
    conn, library_id: int, version_id: int, *, include_ambiguous: bool = False
) -> int:
    """批量重建分层特征节点，返回节点数。"""
    from libref.mining import feature_tree as _tree
    from libref.mining import scope_key as _scope_key

    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    if not include_ambiguous:
        rows = [r for r in rows if r[3] != "AMBIGUOUS"]
    calls = [(str(r[0]), _scope_key(str(r[1]), r[2])) for r in rows if r[0]]
    if not calls:
        conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
        return 0
    kinds = {
        str(r[0]): str(r[1])
        for r in conn.execute(
            "SELECT qualified_name, kind FROM api_entities WHERE version_id=?",
            (version_id,),
        ).fetchall()
    }
    nodes = _tree.build_feature_tree(calls, kinds)
    wanted = [n["path"] for n in nodes]
    for node in nodes:
        conn.execute(
            "INSERT INTO feature_nodes(library_id, version_id, path, parent, depth, kind, calls, scopes)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(version_id, path) DO UPDATE SET parent=excluded.parent,"
            " depth=excluded.depth, kind=excluded.kind,"
            " calls=excluded.calls, scopes=excluded.scopes",
            (
                library_id,
                version_id,
                node["path"],
                node["parent"],
                int(node["depth"]),
                node["kind"],
                int(node["calls"]),
                int(node["scopes"]),
            ),
        )
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        conn.execute(
            "DELETE FROM feature_nodes WHERE version_id=?"
            f" AND path NOT IN ({placeholders})",
            (version_id, *wanted),
        )
    else:
        conn.execute("DELETE FROM feature_nodes WHERE version_id=?", (version_id,))
    return len(nodes)


def refresh_clusters(
    conn,
    library_id: int,
    version_id: int,
    *,
    include_ambiguous: bool = False,
    threshold: float = 0.15,
    min_size: int = 2,
) -> int:
    """批量重建接口分组，返回分组数。"""
    from libref.mining import build_scope_sets as _scope_sets
    from libref.mining.cluster import cluster_apis as _cluster_fn

    rows = conn.execute(
        "SELECT callee_name, caller_file, caller, confidence FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    if not include_ambiguous:
        rows = [r for r in rows if r[3] != "AMBIGUOUS"]
    triplets = [(str(r[0]), str(r[1]), r[2]) for r in rows if r[0]]
    sets = _scope_sets(triplets)
    groups = _cluster_fn(sets, threshold=threshold, min_size=min_size)
    wanted = sorted({m for g in groups for m in g["members"]})
    if not wanted:
        conn.execute("DELETE FROM api_clusters WHERE version_id=?", (version_id,))
        return 0
    for group in groups:
        for member in group["members"]:
            conn.execute(
                "INSERT INTO api_clusters(library_id, version_id, cluster_id, member, size, representative)"
                " VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(version_id, member) DO UPDATE SET cluster_id=excluded.cluster_id,"
                " size=excluded.size, representative=excluded.representative",
                (
                    library_id,
                    version_id,
                    int(group["cluster_id"]),
                    member,
                    int(group["size"]),
                    group["representative"],
                ),
            )
    placeholders = ",".join("?" for _ in wanted)
    conn.execute(
        "DELETE FROM api_clusters WHERE version_id=?"
        f" AND member NOT IN ({placeholders})",
        (version_id, *wanted),
    )
    return len(groups)


def mine_patterns(
    conn,
    library: str,
    version: str,
    *,
    minsup: int = 2,
    topn: int | None = None,
    maxlen: int = 10,
    minlen: int = 2,
    include_ambiguous: bool = False,
    cluster_threshold: float = 0.15,
    cluster_min_size: int = 2,
) -> list[dict]:
    import json as _json

    from libref.mining import PrefixSpan
    from libref.mining import frequency as _freq
    from libref.mining import normalize as _norm
    from libref.mining import scope_key as _scope_key
    from libref.services.search import SearchService

    search = SearchService(conn)
    library_id, version_id = search.ensure_library_version(library, version)
    rows = conn.execute(
        "SELECT callee_name, caller_file, line_start, caller, confidence,"
        " call_order, start_byte, id FROM call_instances"
        " WHERE version_id=?",
        (version_id,),
    ).fetchall()
    if not rows:
        _clear_derived(conn, version_id)
        conn.commit()
        return []
    if not include_ambiguous:
        rows = [r for r in rows if r[4] != "AMBIGUOUS"]
        if not rows:
            _clear_derived(conn, version_id)
            conn.commit()
            return []
    groups: dict[str, list[tuple[int, int, int, int, str]]] = {}
    for callee, caller_file, line_start, caller, _conf, order, start, row_id in rows:
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
        conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
        refresh_feature_tree(
            conn, library_id, version_id, include_ambiguous=include_ambiguous
        )
        refresh_clusters(
            conn,
            library_id,
            version_id,
            include_ambiguous=include_ambiguous,
            threshold=cluster_threshold,
            min_size=cluster_min_size,
        )
        conn.commit()
        return []
    if topn is not None:
        ranked = _freq.exact_counts([n for seq in str_seqs for n in seq])
        allowed = _freq.top_exact_names(ranked, topn)
        str_seqs = [[n for n in seq if n in allowed] for seq in str_seqs]
        str_seqs = [seq for seq in str_seqs if seq]
        if not str_seqs:
            conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
            refresh_feature_tree(
                conn, library_id, version_id, include_ambiguous=include_ambiguous
            )
            refresh_clusters(
                conn,
                library_id,
                version_id,
                include_ambiguous=include_ambiguous,
                threshold=cluster_threshold,
                min_size=cluster_min_size,
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
    for support, pattern in mined or []:
        names = _norm.decode_pattern(list(pattern), reverse)
        payload = _json.dumps(names, ensure_ascii=False)
        wanted.append(payload)
        conn.execute(
            "INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)"
            " VALUES(?, ?, ?, ?, 1)"
            " ON CONFLICT(version_id, pattern) DO UPDATE SET support=excluded.support,"
            " is_closed=1",
            (library_id, version_id, payload, int(support)),
        )
        results.append({"pattern": names, "support": int(support), "is_closed": 1})
    if wanted:
        placeholders = ",".join("?" for _ in wanted)
        conn.execute(
            f"DELETE FROM seq_patterns WHERE version_id=?"
            f" AND pattern NOT IN ({placeholders})",
            (version_id, *wanted),
        )
    else:
        conn.execute("DELETE FROM seq_patterns WHERE version_id=?", (version_id,))
    refresh_feature_tree(
        conn, library_id, version_id, include_ambiguous=include_ambiguous
    )
    refresh_clusters(
        conn,
        library_id,
        version_id,
        include_ambiguous=include_ambiguous,
        threshold=cluster_threshold,
        min_size=cluster_min_size,
    )
    conn.commit()
    results.sort(key=lambda r: (-r["support"], r["pattern"]))
    return results


def build_repo_runner(settings, conn, repos_dir=None):
    async def runner(job) -> None:
        if job.kind == INDEX_REPO_JOB_KIND:
            p = job.params
            index_repo(
                conn,
                p["library"],
                p["version"],
                p["source"],
                update=bool(p.get("update", False)),
                repos_dir=repos_dir,
            )
        elif job.kind == MINE_PATTERNS_JOB_KIND:
            p = job.params
            mine_patterns(
                conn,
                p["library"],
                p["version"],
                minsup=int(p.get("minsup", 2)),
                topn=p.get("topn"),
                maxlen=int(p.get("maxlen", 10)),
                minlen=int(p.get("minlen", 2)),
                include_ambiguous=bool(p.get("include_ambiguous", False)),
                cluster_threshold=float(p.get("cluster_threshold", 0.15)),
                cluster_min_size=int(p.get("cluster_min_size", 2)),
            )
        else:
            raise ValueError(f"unsupported job kind: {job.kind}")

    return runner
