"""Paths and thresholds shared by real usage-mining E2E steps."""

from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent
MANIFEST = HERE / "fixtures" / "usage_mining" / "repos.json"
LOCK = HERE / "fixtures" / "usage_mining" / "repos.lock.json"
DEFAULT_OUT = HERE / "outputs" / "usage_mining_real"
PROFILES = {
    "dpdk-official-examples": "official_example",
    "dpdk-on-mtl": "reference_project",
}
ROLE_OF_PROFILE = {"implementation": "implementation", "usage": "*", **PROFILES}
MINE_PARAMS = {"minsup": 3, "minlen": 2, "maxlen": 8, "topn": 50}
MAX_REPORT_PATTERNS = 50
REVIEW_THRESHOLDS = {
    "unresolved_rate": 0.75,
    "prefix_candidate_rate": 0.60,
    "macro_target_call_ratio": 0.02,
    "macro_definition_coverage": 0.01,
}
