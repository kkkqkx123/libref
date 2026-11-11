"""Command-line exit: only argument parsing plus service calls plus result printing."""

from __future__ import annotations

import sys

from libref.cli.parsers import build_parser
from libref.config import ConfigError

__all__ = ["build_parser", "main"]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
