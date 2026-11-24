"""Library discovery: fuzzy search across registries with deterministic ranking."""

from __future__ import annotations

from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import rank_candidates
from libref.services.registry.discovery.service import DiscoveryService

__all__ = ["DiscoveryCandidate", "DiscoveryService", "rank_candidates"]
