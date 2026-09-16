"""Layered configuration for Mongo2SQLConverter."""

from __future__ import annotations

from datetime import date
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
    "auth",
    "username",
    "password",
    "encrypt",
    "trust_certificate",
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


def _write_local(local: dict[str, Any]) -> Path:
    LOCAL_CONFIG_PATH.write_text(
        yaml.safe_dump(local, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return LOCAL_CONFIG_PATH


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

    return _write_local(local)


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
    return _write_local(local)


# --------------------------------------------------------------------------
# transfer preferences (date range + column selection, per collection)
# --------------------------------------------------------------------------

TIMEZONE_MODES = ("local", "utc")
SCHEDULE_MODES = ("auto", "full", "incremental")


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _as_path_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({str(item) for item in value if item})


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _as_schedule_mode(value: Any) -> str:
    text = str(value or "auto").strip().lower()
    return text if text in SCHEDULE_MODES else "auto"


def default_transfer_prefs() -> dict[str, Any]:
    return {
        "date_filter": {
            "enabled": False,
            "field": "",
            "start": None,
            "end": None,
            "timezone": "local",
        },
        "columns": {"exclude": [], "exclude_tables": []},
        "nesting": "hybrid",
        "table": "",
        "schema": "",
        "schedule_mode": "auto",
        "batch": 500,
        "sample": 5000,
        "allow_null": True,
    }


def load_transfer_prefs(collection: str) -> dict[str, Any]:
    """
    Saved job settings for one collection (date range, columns, schedule).

    Always returns the full shape with defaults, so the UI can read it without
    guarding for missing keys. `exclude` holds Mongo field paths and
    `exclude_tables` holds child-table source paths.
    """
    prefs = default_transfer_prefs()
    stored = (_read_yaml(LOCAL_CONFIG_PATH).get("transfer") or {}).get(collection)
    if not isinstance(stored, dict):
        return prefs

    saved_filter = stored.get("date_filter")
    if isinstance(saved_filter, dict):
        timezone_mode = str(saved_filter.get("timezone") or "local")
        prefs["date_filter"] = {
            "enabled": bool(saved_filter.get("enabled")),
            "field": str(saved_filter.get("field") or ""),
            "start": _as_date(saved_filter.get("start")),
            "end": _as_date(saved_filter.get("end")),
            "timezone": timezone_mode if timezone_mode in TIMEZONE_MODES else "local",
        }

    saved_columns = stored.get("columns")
    if isinstance(saved_columns, dict):
        prefs["columns"] = {
            "exclude": _as_path_list(saved_columns.get("exclude")),
            "exclude_tables": _as_path_list(saved_columns.get("exclude_tables")),
        }

    nesting = str(stored.get("nesting") or "").strip()
    if nesting:
        prefs["nesting"] = nesting
    prefs["table"] = str(stored.get("table") or "").strip()
    prefs["schema"] = str(stored.get("schema") or "").strip()
    prefs["schedule_mode"] = _as_schedule_mode(stored.get("schedule_mode"))
    prefs["batch"] = _as_int(stored.get("batch"), 500, 100)
    prefs["sample"] = _as_int(stored.get("sample"), 5000, 0)
    if "allow_null" in stored:
        prefs["allow_null"] = bool(stored.get("allow_null"))
    return prefs


def save_transfer_prefs(collection: str, prefs: dict[str, Any]) -> Path:
    if not collection:
        return LOCAL_CONFIG_PATH
    defaults = default_transfer_prefs()
    saved_filter = prefs.get("date_filter") or {}
    saved_columns = prefs.get("columns") or {}
    start = _as_date(saved_filter.get("start"))
    end = _as_date(saved_filter.get("end"))
    timezone_mode = str(saved_filter.get("timezone") or "local")

    local = _read_yaml(LOCAL_CONFIG_PATH)
    transfer = dict(local.get("transfer") or {})
    transfer[collection] = {
        "date_filter": {
            "enabled": bool(saved_filter.get("enabled")),
            "field": str(saved_filter.get("field") or ""),
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "timezone": timezone_mode if timezone_mode in TIMEZONE_MODES else "local",
        },
        "columns": {
            "exclude": _as_path_list(saved_columns.get("exclude")),
            "exclude_tables": _as_path_list(saved_columns.get("exclude_tables")),
        },
        "nesting": str(prefs.get("nesting") or defaults["nesting"]),
        "table": str(prefs.get("table") or "").strip(),
        "schema": str(prefs.get("schema") or "").strip(),
        "schedule_mode": _as_schedule_mode(prefs.get("schedule_mode")),
        "batch": _as_int(prefs.get("batch"), defaults["batch"], 100),
        "sample": _as_int(prefs.get("sample"), defaults["sample"], 0),
        "allow_null": bool(prefs["allow_null"]) if "allow_null" in prefs else True,
    }
    local["transfer"] = transfer
    return _write_local(local)
