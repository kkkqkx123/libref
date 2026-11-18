"""CLI request command parsing and remote request mapping."""

from __future__ import annotations

from libref.cli.parsers import build_parser


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
            "--remote",
        ]
    )
    assert args.request_action == "submit"
    assert args.kind == "repo"
    assert args.source == "https://github.com/org/repo.git"
    assert args.remote is True


def test_request_review_parser() -> None:
    parser = build_parser()
    approve = parser.parse_args(["request", "approve", "42"])
    assert approve.request_action == "approve"
    assert approve.request_id == 42
    reject = parser.parse_args(["request", "reject", "7", "--reason", "duplicate"])
    assert reject.reason == "duplicate"
    wait = parser.parse_args(["request", "wait", "9", "--timeout", "1.5"])
    assert wait.timeout == 1.5
