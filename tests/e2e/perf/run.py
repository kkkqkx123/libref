"""Command line entry for the offline performance benchmark."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from tests.e2e.perf import harness

OUTPUTS = harness.OUTPUTS


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="run libref performance benchmarks")
    parser.add_argument(
        "--gear",
        choices=("smoke", "corpus", "exit", "all"),
        default="all",
        help="case group to run; smoke is the fastest offline self-check",
    )
    parser.add_argument("--case", action="append", help="run only this case id")
    parser.add_argument(
        "--baseline",
        default=str(OUTPUTS / "baseline.json"),
        help="baseline report path; missing baselines are reported but do not fail",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="overwrite the baseline with this run",
    )
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="fail when a comparable case is marked needs_review",
    )
    return parser.parse_args()


def _selected(spec: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    cases = spec["cases"]
    if args.case:
        unknown = [name for name in args.case if name not in cases]
        if unknown:
            raise SystemExit(f"unknown cases: {unknown}")
        return {name: cases[name] for name in args.case}
    if args.gear == "all":
        return cases
    return {name: cfg for name, cfg in cases.items() if cfg.get("gear") == args.gear}


def main() -> int:
    args = _parse_args()
    spec = harness.load_spec()
    selected = _selected(spec, args)
    if not selected:
        raise SystemExit("no cases selected")
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    baseline_path = Path(args.baseline)
    baseline: dict[str, Any] | None = None
    if baseline_path.is_file() and not args.save_baseline:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    ctx: dict[str, Any] = {}
    report: dict[str, Any] = {
        "tool": "libref-perf",
        "version": int(spec.get("version", 1)),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gear": args.gear,
        "environment": harness.environment(),
        "settings": {},
        "fixtures": {},
        "cases": [],
        "failures": [],
    }
    for case_id, cfg in selected.items():
        runner = harness.CASES.get(case_id)
        if runner is None:
            raise SystemExit(f"case has no runner: {case_id}")
        results = runner(cfg, ctx)
        report["cases"].extend(results)

    harness.compare(report["cases"], baseline)
    if ctx.get("manifest"):
        report["fixtures"] = {
            name: {"revision": info.get("revision"), "kind": info.get("kind")}
            for name, info in ctx["manifest"]["fixtures"].items()
        }
    dataset = ctx.get("dataset")
    if dataset is not None:
        settings = ctx.get("settings") or harness._bench_settings(dataset)
        report["settings"] = harness.settings_snapshot(settings)
    for case in report["cases"]:
        for failure in case.get("failures", []):
            report["failures"].append(f"{case['case_id']}/{case['variant']}: {failure}")
        if args.enforce and case.get("verdict") == "needs_review":
            report["failures"].append(
                f"{case['case_id']}/{case['variant']}: performance needs review"
            )

    target = harness.write_report(report)
    if args.save_baseline:
        baseline_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"report: {target}")
    for case in report["cases"]:
        item = case.get("stats") or {}
        print(
            f"{case['case_id']}/{case['variant']}: "
            f"median={item.get('median_ms', 0):.4f}ms "
            f"p95={item.get('p95_ms', 0):.4f}ms "
            f"verdict={case.get('verdict', 'observed')}"
        )
    for failure in report["failures"]:
        print(f"FAIL {failure}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
