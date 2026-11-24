"""Job runners for mining and label induction."""

from __future__ import annotations

from libref.services.code.mining_refresh.induction import induce_labels
from libref.services.code.mining_refresh.params import (
    DEFAULT_MAXLEN,
    DEFAULT_MINLEN,
    DEFAULT_MINSUP,
    INDUCE_LABELS_JOB_KIND,
    MINE_PATTERNS_JOB_KIND,
)
from libref.services.code.mining_refresh.patterns import mine_patterns


def build_mine_runner(settings, conn):
    """Build the mining job consumer function, handling only mine_patterns, decoupled from source indexing."""

    async def runner(job):
        if job.kind != MINE_PATTERNS_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        return mine_patterns(
            conn,
            p["library"],
            p["version"],
            ecosystem=p.get("ecosystem"),
            minsup=int(p.get("minsup", DEFAULT_MINSUP)),
            topn=p.get("topn"),
            maxlen=int(p.get("maxlen", DEFAULT_MAXLEN)),
            minlen=int(p.get("minlen", DEFAULT_MINLEN)),
            include_ambiguous=bool(p.get("include_ambiguous", False)),
            include_unconfirmed=bool(p.get("include_unconfirmed", False)),
            cluster_threshold=float(
                p.get("cluster_threshold")
                if p.get("cluster_threshold") is not None
                else settings.cluster_threshold
            ),
            cluster_min_size=int(
                p.get("cluster_min_size")
                if p.get("cluster_min_size") is not None
                else settings.cluster_min_size
            ),
            resolve_kinds=p.get("resolve_kinds"),
            min_args=p.get("min_args"),
            max_args=p.get("max_args"),
            profile=p.get("profile"),
            source_roles=p.get("source_roles"),
            relation_kinds=p.get("relation_kinds"),
            macro_policy=p.get("macro_policy"),
        )

    return runner


def build_induce_runner(settings, conn):
    """Build the induction job consumer."""

    async def runner(job):
        if job.kind != INDUCE_LABELS_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        return await induce_labels(
            conn,
            settings,
            p["library"],
            p["version"],
            ecosystem=p.get("ecosystem"),
            topn=p.get("topn"),
            include_ambiguous=bool(p.get("include_ambiguous", False)),
            include_unconfirmed=bool(p.get("include_unconfirmed", False)),
            profile=p.get("profile"),
            macro_policy=p.get("macro_policy"),
        )

    return runner
