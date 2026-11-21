"""Registry commands: discover, catalog, resolve, search-web."""

from __future__ import annotations

import argparse

from libref.cli.common import (
    _connect,
    _load_settings,
    _print_suggestions,
    as_error,
)
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
            raise as_error(exc) from exc
        except RegistryError as exc:
            raise as_error(exc) from exc
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
            raise as_error(exc) from exc
        except RegistryError as exc:
            raise as_error(exc) from exc
        if not candidates:
            print("(no suggestions)")
            return 0
        print("suggestions:")
        for c in candidates:
            print(_json.dumps(c, ensure_ascii=False))
        return 0
    finally:
        conn.close()


def _remote_catalog(args: argparse.Namespace, settings) -> int:
    import json as _json

    import httpx as _httpx

    from libref.cli.remote import auth_headers as _ah
    from libref.cli.remote import base_url_for as _buf

    base = _buf(args, settings)
    params = {}
    if getattr(args, "library", None) is not None:
        params["library"] = args.library
    if getattr(args, "ecosystem", None) is not None:
        params["ecosystem"] = args.ecosystem
    try:
        with _httpx.Client(timeout=30.0) as _c:
            _r = _c.get(f"{base}/catalog", params=params, headers=_ah(settings))
            _r.raise_for_status()
            data = _r.json()
    except Exception as exc:
        raise as_error(exc) from exc
    for r in data.get("items") or []:
        print(_json.dumps(r, ensure_ascii=False))
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    import json

    from libref.services.registry.libraries import LibraryService

    _st = _load_settings(args)
    from libref.cli.remote import is_remote as _is_remote

    if _is_remote(args, _st):
        return _remote_catalog(args, _st)
    conn = _connect(_st)
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
            raise as_error(exc) from exc
        except RegistryError as exc:
            raise as_error(exc) from exc
    finally:
        conn.close()
    print(_json.dumps(resolution.model_dump(), ensure_ascii=False))
    return 0
