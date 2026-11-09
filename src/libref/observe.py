"""Runtime logging assembly: initialize the root logger from the effective config; failures do not block business."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

_configured_fingerprint: str | None = None


def _normalize_level(raw: object) -> int:
    name = str(raw or "WARNING").upper()
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }.get(name, logging.WARNING)


def _text_formatter() -> logging.Formatter:
    return logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        try:
            return json.dumps(payload, ensure_ascii=False)
        except TypeError, ValueError:
            return json.dumps(
                {
                    "time": payload["time"],
                    "level": payload["level"],
                    "logger": payload["logger"],
                    "message": str(record.msg)[:500],
                }
            )


def _our_handlers() -> list[logging.Handler]:
    return [h for h in logging.root.handlers if getattr(h, "libref_managed", False)]


def setup_logging(settings) -> None:
    """Initialize logging from the effective config; idempotent and never raises."""
    global _configured_fingerprint
    try:
        level = _normalize_level(getattr(settings, "log_level", "WARNING"))
        use_json = (
            str(getattr(settings, "log_format", "text") or "text").lower() == "json"
        )
        log_file = getattr(settings, "log_file", None)
        fingerprint = f"{level}|{use_json}|{log_file}"
        if fingerprint == _configured_fingerprint:
            return
        for handler in _our_handlers():
            try:
                logging.root.removeHandler(handler)
                handler.close()
            except Exception:  # noqa: BLE001, S112 — failing to clean up old output does not block
                continue
        formatter: logging.Formatter = (
            _JsonFormatter() if use_json else _text_formatter()
        )
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.libref_managed = True  # type: ignore[attr-defined]
        logging.root.addHandler(stream)
        if log_file is not None:
            try:
                path = Path(log_file).expanduser()
                if not path.is_absolute():
                    path = Path.cwd() / path
                path.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.handlers.RotatingFileHandler(
                    path, maxBytes=5 * 1024 * 1024, backupCount=1, encoding="utf-8"
                )
                file_handler.setFormatter(formatter)
                file_handler.libref_managed = True  # type: ignore[attr-defined]
                logging.root.addHandler(file_handler)
            except OSError:
                logging.getLogger(__name__).warning("log file unavailable, stderr only")
        logging.root.setLevel(level)
        _configured_fingerprint = fingerprint
    except Exception:  # noqa: BLE001, S110 — logging assembly never blocks business
        pass
