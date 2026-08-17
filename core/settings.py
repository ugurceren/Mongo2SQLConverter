"""Layered configuration for Mongo2SQLConverter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
LOCAL_CONFIG_PATH = ROOT / "config.local.yaml"

MONGO_KEYS = ("uri", "database", "username", "password")
MSSQL_KEYS = (
    "server",
    "database",
    "schema",
    "driver",
    "trusted_connection",
    "username",
    "password",
)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(config_path: Path | str | None = None) -> dict[str, Any]:
    base_path = Path(config_path) if config_path else CONFIG_PATH
    if not base_path.is_absolute():
        base_path = ROOT / base_path
    return deep_merge(_read_yaml(base_path), _read_yaml(LOCAL_CONFIG_PATH))


def load_connection_overrides() -> dict[str, Any]:
    return _read_yaml(LOCAL_CONFIG_PATH)


def save_connection_overrides(
    mongodb: dict[str, Any], mssql: dict[str, Any]
) -> Path:
    local = _read_yaml(LOCAL_CONFIG_PATH)
    for section, values, allowed in (
        ("mongodb", mongodb, MONGO_KEYS),
        ("mssql", mssql, MSSQL_KEYS),
    ):
        target = dict(local.get(section) or {})
        for key in allowed:
            if key not in values:
                continue
            value = values[key]
            if value is None:
                continue
            target[key] = value
        local[section] = target

    LOCAL_CONFIG_PATH.write_text(
        yaml.safe_dump(local, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return LOCAL_CONFIG_PATH


def load_sync_watermark(collection: str) -> dict[str, str] | None:
    state = (_read_yaml(LOCAL_CONFIG_PATH).get("sync") or {}).get(collection)
    if not isinstance(state, dict) or not state.get("last_id"):
        return None
    return {
        "last_id": str(state["last_id"]),
        "last_id_type": str(state.get("last_id_type") or "objectid"),
        "updated": str(state.get("updated") or ""),
    }


def save_sync_watermark(collection: str, last_id: str, last_id_type: str) -> Path:
    from datetime import datetime, timezone

    local = _read_yaml(LOCAL_CONFIG_PATH)
    sync = dict(local.get("sync") or {})
    sync[collection] = {
        "last_id": last_id,
        "last_id_type": last_id_type,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    local["sync"] = sync
    LOCAL_CONFIG_PATH.write_text(
        yaml.safe_dump(local, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return LOCAL_CONFIG_PATH
