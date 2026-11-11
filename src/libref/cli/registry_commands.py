"""Registry commands: discover, catalog, resolve, search-web."""

from __future__ import annotations

import argparse
import sys

from libref.cli.common import _connect, _load_settings, _print_suggestions
from libref.crawler.registry import RegistryError


def cmd_discover(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        return _print_suggestions(conn, settings, args)
    finally:
        conn.close()


def cmd_search_web(args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import json as _json

    from libref.services.factory import build_discovery_service

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            candidates = _asyncio.run(
                build_discovery_service(conn, settings).search_web_async(
                    args.query, args.limit
                )
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        except RegistryError as exc:
            print(f"discovery failed [{exc.kind}]: {exc}", file=sys.stderr)
            return 1
        if not candidates:
            print("(no suggestions)")
            return 0
        print("suggestions:")
        for c in candidates:
            print(_json.dumps(c, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def cmd_search_github(args: argparse.Namespace) -> int:
    import asyncio as _asyncio
    import json as _json

    from libref.services.factory import build_discovery_service

    settings = _load_settings(args)
    conn = _connect(settings)
    try:
        try:
            candidates = _asyncio.run(
                build_discovery_service(conn, settings).search_github_async(
                    args.query, args.limit
                )
            )
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        except RegistryError as exc:
            print(f"discovery failed [{exc.kind}]: {exc}", file=sys.stderr)
            return 1
        if not candidates:
            print("(no suggestions)")
            return 0
        print("suggestions:")
        for c in candidates:
            print(_json.dumps(c, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def cmd_catalog(args: argparse.Namespace) -> int:
    import json

    from libref.services.registry.libraries import LibraryService

    conn = _connect(_load_settings(args))
    try:
        rows = LibraryService(conn).catalog(
            getattr(args, "library", None),
            getattr(args, "ecosystem", None),
        )
    finally:
        conn.close()
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    import asyncio
    import json as _json

    from libref.crawler.http import build_async_client
    from libref.services.registry.resolver import resolve

    settings = _load_settings(args)
    conn = _connect(settings)
    try:

        async def _run():
            async with build_async_client(settings) as client:
                return await resolve(
                    conn,
                    client,
                    settings,
                    args.ecosystem,
                    args.name,
                    getattr(args, "version", None),
                    getattr(args, "target", "docs"),
                    bool(getattr(args, "include_prerelease", False)),
                )

        try:
            resolution = asyncio.run(_run())
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        except RegistryError as exc:
            from libref.cli.common import format_registry_error

            print(
                format_registry_error(exc, target=getattr(args, "target", "docs")),
                file=sys.stderr,
            )
            return 1
    finally:
        conn.close()
    print(_json.dumps(resolution.model_dump(), ensure_ascii=False))
    return 0
