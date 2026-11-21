"""Command-line exit: only argument parsing plus service calls plus result printing."""

from __future__ import annotations

from libref.cli.common import report_failure
from libref.cli.parsers import build_parser
from libref.errors import normalize

__all__ = ["build_parser", "main"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        found = normalize(exc)
        if found is None:
            raise
        return report_failure(found, args)
