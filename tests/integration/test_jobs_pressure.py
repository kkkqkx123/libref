"""Multi-replica pressure: concurrent claim, heartbeat timeout and recovery.

One submission must be claimed exactly once no matter how many replicas race
for it. The same script runs on the embedded backend always and on the shared
PostgreSQL backend when ``LIBREF_TEST_POSTGRES_URL`` provides a server, so a
second replica set can share the same rows.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

from libref.db.assembly import build_metadata_session
from libref.services.infra.jobs import JobManager

_PG_URL = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")

needs_pg = pytest.mark.skipif(not _PG_URL, reason="LIBREF_TEST_POSTGRES_URL not set")


class _SqliteSettings:
    metadata_backend = "sqlite"
    fulltext_backend = "fts5"
    db_path = ""


class _PostgresSettings:
    metadata_backend = "postgres"
    fulltext_backend = "postgres"
    postgres_url = _PG_URL
    postgres_pool_size = 8
    postgres_timeout = 10.0


def _fresh_postgres(url: str) -> None:
    import psycopg

    parsed = psycopg.conninfo.conninfo_to_dict(url)
    dbname = (parsed.get("dbname") or "postgres").replace('"', "")
    admin = dict(parsed, dbname="postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')


def _sqlite_session(tmp_path, name: str):
    settings = _SqliteSettings()
    settings.db_path = str(tmp_path / f"{name}.db")
    session = build_metadata_session(settings)
    session.apply_migrations()
    return session


def _pg_session(fresh: bool = True):
    if fresh:
        _fresh_postgres(_PG_URL)
    settings = _PostgresSettings()
    session = build_metadata_session(settings)
    session.apply_migrations()
    return session


def _run_claim_race(make_session, threads: int = 8, jobs: int = 64) -> dict[int, int]:
    """Submit jobs once, race thread-local sessions for them, count claims per thread."""
    seed = make_session()
    try:
        wanted = [JobManager(seed).submit(f"pressure-{i}") for i in range(jobs)]
    finally:
        seed.close()
    barrier = threading.Barrier(threads)
    per_thread: dict[int, int] = {}
    claimed: list[int] = []
    guard = threading.Lock()
    deadline = time.monotonic() + 60.0

    def work(index: int) -> None:
        session = make_session()
        try:
            manager = JobManager(session, instance_id=f"replica-{index}")
            barrier.wait(timeout=10.0)
            mine = 0
            while True:
                with guard:
                    if len(claimed) >= jobs:
                        break
                if time.monotonic() > deadline:
                    break
                # A lost race returns None while rows remain, exactly like a
                # quiet poll in production: back off and retry, exit only when
                # the queue is observably drained.
                job = manager.claim_next()
                if job is None:
                    time.sleep(0.01)
                    continue
                mine += 1
                with guard:
                    claimed.append(job.id)
                # Model job execution between claims: production workers never
                # tight-loop claim_next, and without this gap one timeslice
                # drains the whole burst before peers wake, which proves
                # nothing about contention. The sleep yields the race window.
                time.sleep(0.002)
        finally:
            session.close()
        with guard:
            per_thread[index] = mine

    workers = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=60.0)
    assert not any(w.is_alive() for w in workers)
    # Hard gates: every submission claimed exactly once, no double-run.
    assert sorted(claimed) == sorted(wanted)
    assert len(set(claimed)) == jobs
    # Fairness is informational: polling claim is winner-takes-most under a
    # burst by design (whoever polls first wins); production spacing comes
    # from real job execution between claims, modelled by the sleep above.
    print(f"claim distribution over {threads} replicas: {per_thread}")
    return per_thread


def _run_stale_recovery(make_session) -> None:
    session = make_session()
    try:
        manager = JobManager(session, stale_after=0.3, instance_id="replica-0")
        first = manager.submit("stale-one")
        assert manager.claim_next() is not None
        time.sleep(0.5)
        assert manager.recover() == [first]
        assert manager.get(first) is not None
        assert manager.get(first).status == "failed"

        second = manager.submit("stale-two")
        assert manager.claim_next() is not None
        time.sleep(0.5)
        assert manager.recover(requeue=True) == [second]
        assert manager.get(second).status == "pending"
        assert manager.claim_next() is not None
        assert manager.get(second).status == "running"
    finally:
        session.close()


def test_claim_race_sqlite(tmp_path) -> None:
    _run_claim_race(lambda: _sqlite_session(tmp_path, "race"))


@needs_pg
def test_claim_race_postgres() -> None:
    fresh = True

    def make_session():
        nonlocal fresh
        session = _pg_session(fresh=fresh)
        fresh = False
        return session

    _run_claim_race(make_session)


def test_stale_recovery_sqlite(tmp_path) -> None:
    _run_stale_recovery(lambda: _sqlite_session(tmp_path, "stale"))


@needs_pg
def test_stale_recovery_postgres() -> None:
    _run_stale_recovery(_pg_session)
