"""Settings loader (D13): schema.yaml declares, willie.yaml holds values.

Live file location:
  * $WILLIE_CONFIG if set, else ~/.config/willie/willie.yaml
  * created from repo config/willie.yaml (or schema defaults) on first start
This keeps `make deploy` from overwriting settings changed in the dashboard.
"""
from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("willie.config")

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "config" / "schema.yaml"
REPO_DEFAULTS = REPO / "config" / "willie.yaml"
LIVE_PATH = Path(os.environ.get("WILLIE_CONFIG", Path.home() / ".config" / "willie" / "willie.yaml"))

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def fields(schema: dict):
    """Yield (section, key, spec) for every setting."""
    for sec, items in schema.items():
        for key, spec in items.items():
            if not key.startswith("_"):
                yield sec, key, spec


def defaults(schema: dict) -> dict:
    out: dict[str, dict] = {}
    for sec, key, spec in fields(schema):
        out.setdefault(sec, {})[key] = spec.get("default")
    return out


def coerce(spec: dict, value: Any) -> Any:
    """Convert + validate one value. Raises ValueError with a readable message."""
    t = spec["type"]
    if t == "bool":
        if isinstance(value, bool):
            v = value
        elif str(value).lower() in ("1", "true", "yes", "on"):
            v = True
        elif str(value).lower() in ("0", "false", "no", "off"):
            v = False
        else:
            raise ValueError("must be true/false")
        return v
    if t in ("int", "float"):
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if t == "int":
            if v != int(v):
                raise ValueError("must be a whole number")
            v = int(v)
        if "min" in spec and v < spec["min"]:
            raise ValueError(f"minimum is {spec['min']}")
        if "max" in spec and v > spec["max"]:
            raise ValueError(f"maximum is {spec['max']}")
        return v
    if t == "choice":
        if value not in spec["options"]:
            raise ValueError(f"must be one of {spec['options']}")
        return value
    if t == "time":
        if not _TIME_RE.match(str(value)):
            raise ValueError("must be HH:MM")
        return str(value)
    return "" if value is None else str(value)


class Config:
    """Thread-safe settings store with validation, atomic save and hot reload."""

    def __init__(self, path: Path = LIVE_PATH):
        self.path = Path(path)
        self.schema = load_schema()
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._listeners = []
        self.values = defaults(self.schema)
        self._ensure_file()
        self.reload(force=True)

    # ---- file handling -------------------------------------------------
    def _ensure_file(self):
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if REPO_DEFAULTS.exists():
            shutil.copy(REPO_DEFAULTS, self.path)
        else:
            self._write(self.values)
        log.info("created settings file %s", self.path)

    def _write(self, values: dict):
        header = "# WILL-E live settings — edit in the dashboard (schema: config/schema.yaml)\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".willie-", suffix=".yaml")
        # Group-readable/-writable: the services run as user willie, Wouter's tools as woetner,
        # both in group willie (tools/service_user.sh, security audit 25 Sep).
        os.fchmod(fd, 0o660)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.safe_dump(values, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    def reload(self, force: bool = False) -> bool:
        """Re-read the file if it changed. Invalid values fall back to the default."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if not force and mtime == self._mtime:
            return False
        with open(self.path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        merged = defaults(self.schema)
        for sec, key, spec in fields(self.schema):
            if key in (raw.get(sec) or {}):
                try:
                    merged[sec][key] = coerce(spec, raw[sec][key])
                except ValueError as e:
                    log.warning("settings %s.%s invalid (%s) -> default", sec, key, e)
        with self._lock:
            old, self.values, self._mtime = self.values, merged, mtime
        if not force:
            changed = [f"{sec}.{k}: {old[sec][k]!r} -> {v!r}"
                       for sec, items in merged.items() for k, v in items.items() if old[sec][k] != v]
            log.info("settings reloaded from disk%s", (": " + ", ".join(changed)) if changed else " (no changes)")
        self._notify(old)
        return True

    # ---- access ----------------------------------------------------------
    def get(self, dotted: str):
        sec, key = dotted.split(".", 1)
        with self._lock:
            return self.values[sec][key]

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.values)

    def update(self, changes: dict) -> dict:
        """Apply {section: {key: value}}. All-or-nothing. Returns the new values."""
        spec_of = {(s, k): sp for s, k, sp in fields(self.schema)}
        errors, new = {}, self.snapshot()
        for sec, items in (changes or {}).items():
            for key, value in (items or {}).items():
                spec = spec_of.get((sec, key))
                if spec is None:
                    errors[f"{sec}.{key}"] = "unknown setting"
                    continue
                try:
                    new[sec][key] = coerce(spec, value)
                except ValueError as e:
                    errors[f"{sec}.{key}"] = str(e)
        if errors:
            raise ConfigError(errors)
        with self._lock:
            old = self.values
            self._write(new)
            self.values = new
        for sec, items in changes.items():
            for key in items:
                if old[sec][key] != new[sec][key]:
                    log.info("setting %s.%s: %r -> %r", sec, key, old[sec][key], new[sec][key])
        self._notify(old)
        return self.snapshot()

    # ---- hot reload listeners -------------------------------------------
    def on_change(self, fn):
        """fn(old_values, new_values) is called after every change."""
        self._listeners.append(fn)

    def _notify(self, old):
        for fn in self._listeners:
            try:
                fn(old, self.values)
            except Exception:  # a listener must never break the store
                log.exception("settings listener failed")
