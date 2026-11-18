"""Self-hosted SPA fallback; API routes and /mcp keep priority over the shell.

Registered inside build_web_app for standalone serving. The combined serve
command flattens web routes into one app, which bypasses this inner stack;
it registers the same middleware at the combined level (see cmd_serve)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse


def spa_dir_for(settings) -> Path | None:
    """Resolve the SPA directory when enabled and carrying an entry file."""
    if not bool(getattr(settings, "web_spa_enabled", True)):
        return None
    raw = str(getattr(settings, "web_spa_dir", "") or "")
    if not raw:
        return None
    directory = Path(raw).expanduser()
    if not (directory / "index.html").is_file():
        return None
    return directory


class SpaMiddleware(BaseHTTPMiddleware):
    """Serve the shell for browser navigations that match no API route."""

    def __init__(
        self,
        app: Any,
        *,
        directory: Path,
        mcp_prefix: str,
        api_paths: set[str] | frozenset[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._directory = directory
        self._index = directory / "index.html"
        self._mcp_prefix = "/" + str(mcp_prefix or "/mcp").strip("/")
        self._api_paths = frozenset(api_paths or ())

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response
        if (
            response.status_code in (401, 404)
            and request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
            and path not in self._api_paths
            and path != self._mcp_prefix
            and not path.startswith(self._mcp_prefix + "/")
            and self._index.is_file()
        ):
            return FileResponse(
                self._index,
                headers={"Cache-Control": "no-store"},
            )
        return response
