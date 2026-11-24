"""Configuration file/environment resolution, redaction and fingerprinting."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from libref.config import ConfigError, LibrefSettings
from libref.config_defaults import (
    CONFIG_FILE_NAME,
    MASKED_VALUE,
    PATH_FIELDS,
    RESERVED_SECTIONS,
    SECRET_FIELDS,
    SECTION_FIELDS,
    URL_FIELDS,
)


def _mask_url_credentials(value: str) -> str:
    """Replace the password (or whole userinfo) of a URL, keeping scheme, host and path readable."""
    parts = urlsplit(value)
    if parts.username is None and parts.password is None:
        return value
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    auth = f"{parts.username}:{MASKED_VALUE}@" if parts.password else f"{MASKED_VALUE}@"
    return urlunsplit(
        (parts.scheme, auth + host, parts.path, parts.query, parts.fragment)
    )


def resolve_config_file(explicit: str | Path | None = None) -> Path | None:
    """Locate the config file by explicit arg, current directory, then user config dir; None if not found."""
    if explicit is not None:
        candidate = Path(explicit)
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate
    local = Path.cwd() / CONFIG_FILE_NAME
    if local.is_file():
        return local
    home_config = Path.home() / ".config" / "libref" / CONFIG_FILE_NAME
    if home_config.is_file():
        return home_config
    return None


def _flatten_document(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a sectioned document into a field dict; unknown section or key names raise immediately."""
    fields = set(LibrefSettings.model_fields)
    flat: dict[str, Any] = {}
    for name, value in document.items():
        if isinstance(value, dict) and name in SECTION_FIELDS:
            allowed = SECTION_FIELDS[name]
            for key, item in value.items():
                nested = f"{name}.{key}"
                if isinstance(item, dict) and nested in SECTION_FIELDS:
                    for sub, sub_item in item.items():
                        if sub not in SECTION_FIELDS[nested]:
                            raise ConfigError(
                                f"unknown config key '{nested}.{sub}' in {path}"
                            )
                        _reject_secret(path, sub)
                        flat[sub] = sub_item
                    continue
                if key not in allowed:
                    raise ConfigError(f"unknown config key '{name}.{key}' in {path}")
                _reject_secret(path, key)
                flat[key] = item
        elif isinstance(value, dict) and name in RESERVED_SECTIONS:
            if value:
                raise ConfigError(
                    f"config section [{name}] in {path} is reserved and must stay empty"
                )
        elif name in fields:
            _reject_secret(path, name)
            flat[name] = value
        elif isinstance(value, dict):
            raise ConfigError(f"unknown config section [{name}] in {path}")
        else:
            raise ConfigError(f"unknown config key '{name}' in {path}")
    return flat


def _reject_secret(path: Path, key: str) -> None:
    if key in SECRET_FIELDS:
        raise ConfigError(
            f"config key '{key}' in {path} must come from the environment, not the file"
        )


def _resolve_relative_paths(values: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve relative paths in the file against the config file's directory; absolute paths stay unchanged."""
    resolved = dict(values)
    for key in PATH_FIELDS & set(values):
        value = values[key]
        if value is None:
            continue
        candidate = Path(str(value)).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        resolved[key] = str(candidate)
    return resolved


def _env_covered_fields() -> set[str]:
    """Fields already provided by env vars or local injected files; same-named file fields yield to them."""
    from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

    fields = set(LibrefSettings.model_fields)
    covered: set[str] = set()
    for source in (
        EnvSettingsSource(LibrefSettings),
        DotEnvSettingsSource(LibrefSettings),
    ):
        for name in source.env_vars:
            if name.startswith("libref_") and name[7:] in fields:
                covered.add(name[7:])
    return covered


def load_settings(
    config: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> LibrefSettings:
    """Unified load entry: locate file, merge three sources, validate, return a frozen snapshot."""
    path = resolve_config_file(config)
    file_values: dict[str, Any] = {}
    if path is not None:
        try:
            with open(path, "rb") as handle:
                document = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config file is not valid TOML: {path}: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"config file is not readable: {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise ConfigError(f"config file must hold tables: {path}")
        file_values = _resolve_relative_paths(
            _flatten_document(path, document), path.parent
        )
        covered = _env_covered_fields()
        file_values = {k: v for k, v in file_values.items() if k not in covered}
    merged: dict[str, Any] = dict(file_values)
    for key, value in dict(overrides or {}).items():
        if key not in LibrefSettings.model_fields:
            raise ConfigError(f"unknown config override '{key}'")
        merged[key] = value
    try:
        return LibrefSettings(**merged)
    except ValidationError as exc:
        origin = f"config file {path}" if path is not None else "environment (LIBREF_*)"
        raise ConfigError(f"invalid configuration from {origin}: {exc}") from exc


def redacted_dict(settings: LibrefSettings) -> dict[str, Any]:
    """Redacted effective snapshot: secrets masked when set, paths as strings, directly serializable."""
    dumped = settings.model_dump()
    for key in SECRET_FIELDS:
        if dumped.get(key):
            dumped[key] = MASKED_VALUE
    for key in URL_FIELDS:
        value = dumped.get(key)
        if isinstance(value, str):
            dumped[key] = _mask_url_credentials(value)
    for key, value in dumped.items():
        if isinstance(value, Path):
            dumped[key] = str(value)
    return dumped


def settings_fingerprint(settings: LibrefSettings) -> str:
    """Digest of the effective snapshot; stable for identical content, differs on any field change."""
    canonical = json.dumps(redacted_dict(settings), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
