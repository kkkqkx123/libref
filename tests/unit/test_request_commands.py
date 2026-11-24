"""Client request parsing and wire mapping."""

from __future__ import annotations

from librefctl.app import _collect, _request_body, build_parser
from librefctl.commands import BY_NAME


def test_request_submit_parser() -> None:
    args = build_parser().parse_args(
        [
            "request",
            "submit",
            "--kind",
            "repo",
            "--library",
            "demo",
            "--version",
            "1.0",
            "--source",
            "https://github.com/org/repo.git",
        ]
    )
    assert args.kind == "repo"
    assert args.source == "https://github.com/org/repo.git"
    assert args._command.name == "request submit"


def test_request_review_parser() -> None:
    parser = build_parser()
    approve = parser.parse_args(["request", "approve", "42"])
    assert approve._command.name == "request approve"
    assert approve.request_id == 42
    reject = parser.parse_args(["request", "reject", "7", "--reason", "duplicate"])
    assert reject.reason == "duplicate"
    wait = parser.parse_args(["request", "wait", "9", "--timeout", "1.5"])
    assert wait.timeout == 1.5


def test_request_submit_builds_target_filter() -> None:
    args = build_parser().parse_args(
        [
            "request",
            "submit",
            "--kind",
            "repo",
            "--library",
            "demo",
            "--target-prefix",
            "rte_",
            "--target-name",
            "rte_write",
        ]
    )
    command = BY_NAME["request submit"]
    _, _, payload, local = _collect(command, args)
    body = _request_body(command, payload, local)
    assert body["target_filter"] == {
        "prefixes": ["rte_"],
        "names": ["rte_write"],
    }


def test_job_create_wraps_params() -> None:
    args = build_parser().parse_args(["mine", "demo", "1.0", "--minsup", "3"])
    command = BY_NAME["mine"]
    _, _, payload, local = _collect(command, args)
    body = _request_body(command, payload, local)
    assert body["kind"] == "mine_patterns"
    assert body["params"]["minsup"] == 3
