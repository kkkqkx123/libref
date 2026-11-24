"""Mining read model facade: frequency, co-occurrence, patterns and insights."""

from __future__ import annotations

from libref.services.code.mining_read.api_usage import ApiUsageReader
from libref.services.code.mining_read.common import ReadSupport, is_subsequence
from libref.services.code.mining_read.feature_read import FeatureReader
from libref.services.code.mining_read.health import HealthReader
from libref.services.code.mining_read.insight import InsightReader
from libref.services.code.mining_read.pattern_read import PatternReader
from libref.services.code.mining_read.symbol_read import SymbolReader

__all__ = ["MiningReadService", "is_subsequence"]


class MiningReadService(
    ReadSupport,
    ApiUsageReader,
    PatternReader,
    FeatureReader,
    SymbolReader,
    HealthReader,
    InsightReader,
):
    """Read-only queries over call_instances, seq_patterns, and derived tables."""

    def __init__(self, conn, settings=None) -> None:
        self._conn = conn
        self._settings = settings
