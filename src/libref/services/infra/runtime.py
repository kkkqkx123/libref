"""Multi-replica runtime support: graceful drain and configuration agreement.

Central deployments run several service replicas against one shared metadata
backend. A draining replica stops accepting new work and lets in-flight jobs
finish before releasing its session; replicas verify the effective settings
fingerprint recorded in `schema_meta` so a mis-configured replica refuses to
join the fleet instead of silently diverging.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

CONFIG_FINGERPRINT_KEY = "config_fingerprint"

_draining = False
_drained_at: float | None = None


def is_draining() -> bool:
    """Whether this replica has been asked to stop accepting work."""
    return _draining


def drain(
    job_manager: Any = None,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.2,
) -> int:
    """Stop accepting new work and wait for in-flight jobs to finish.

    Returns the number of jobs still unfinished when the timeout expired; those
    are recovered by heartbeat recovery on the remaining replicas.
    """
    global _draining, _drained_at

    _draining = True
    _drained_at = time.time()
    logger.info("drain started")
    if job_manager is None:
        return 0
    try:
        job_manager.stop_worker()
    except Exception:
        logger.debug("job worker already stopped", exc_info=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        running = _running_jobs(job_manager)
        if running == 0:
            logger.info("drain complete")
            return 0
        time.sleep(poll_interval)
    remaining = _running_jobs(job_manager)
    if remaining:
        logger.warning("drain timed out with %d job(s) unfinished", remaining)
    return remaining


def _running_jobs(job_manager: Any) -> int:
    try:
        rows = job_manager._conn.execute(
            "SELECT count(*) FROM jobs WHERE status='running'"
        ).fetchone()
    except Exception:  # noqa: BLE001 — drain never blocks on a probe failure
        return 0
    return int(rows[0]) if rows else 0


def check_config_fingerprint(session: Any, settings: Any) -> bool:
    """Verify this replica's settings agree with the fleet fingerprint.

    Writes the fingerprint when absent (first replica boot) and refuses with
    `False` on mismatch. A mismatch is audit-logged so operators see the
    divergence in the shared control plane.
    """
    from libref.config import settings_fingerprint

    fp = settings_fingerprint(settings)
    row = session.execute(
        "SELECT value FROM schema_meta WHERE key=?", (CONFIG_FINGERPRINT_KEY,)
    ).fetchone()
    if row is None:
        session.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES(?, ?)",
            (CONFIG_FINGERPRINT_KEY, fp),
        )
        session.commit()
        return True
    recorded = str(row[0])
    if recorded == fp:
        return True
    logger.error(
        "configuration fingerprint mismatch: replica=%s fleet=%s; refusing to serve",
        fp[:12],
        recorded[:12],
    )
    try:
        session.execute(
            "INSERT INTO audit_log(tenant_id, principal_id, action, target, result)"
            " VALUES(?, NULL, ?, ?, ?)",
            (
                "default",
                "config_fingerprint_mismatch",
                fp,
                "refused",
            ),
        )
        session.commit()
    except Exception:
        logger.debug("audit write for fingerprint mismatch failed", exc_info=True)
    return False
