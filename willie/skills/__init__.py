"""Skill registry (H1, §6): every home system is one small module in this folder.

A skill module exposes:

    LABEL          one line for the dashboard ("Brandstof - health monitor")
    DECLARATIONS   list of tool declarations (same format as voice/tools.py)
    HANDLERS       {tool name: function(**args) -> dict}
    card()         optional: a small dict for the dashboard card (cheap, no network wait)

Modules whose name starts with "_" are not loaded (`_template.py` is the template).
A skill that fails to import is listed with its error and skipped; it never takes the
voice session down.

On/off lives in the settings (`skills.disabled`, comma-separated), so the dashboard
switch survives a restart. A switched-off skill's tools are refused at once and are left
out of the tool list from the next conversation on (a Live session declares its tools
when it starts).
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import threading
from types import ModuleType

log = logging.getLogger("willie.skills")

_lock = threading.Lock()
_modules: dict[str, ModuleType] | None = None
_errors: dict[str, str] = {}
_config = None


def _settings():
    global _config
    if _config is None:
        from willie.config import Config
        _config = Config()
    else:
        _config.reload()
    return _config


def available() -> dict[str, ModuleType]:
    """All importable skill modules, by name (imported once per process)."""
    global _modules
    with _lock:
        if _modules is None:
            _modules = {}
            for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
                if info.name.startswith("_"):
                    continue
                try:
                    module = importlib.import_module(f"{__name__}.{info.name}")
                except Exception as exc:
                    _errors[info.name] = f"{type(exc).__name__}: {exc}"
                    log.warning("skill %s not loaded: %s", info.name, _errors[info.name])
                    continue
                if isinstance(getattr(module, "HANDLERS", None), dict):
                    _modules[info.name] = module
        return dict(_modules)


def disabled() -> set[str]:
    try:
        raw = _settings().get("skills.disabled")
    except Exception:
        return set()
    return {name.strip() for name in str(raw or "").split(",") if name.strip()}


def enabled() -> dict[str, ModuleType]:
    off = disabled()
    return {name: module for name, module in available().items() if name not in off}


def set_enabled(name: str, on: bool) -> None:
    if name not in available():
        raise KeyError(name)
    off = disabled()
    off.discard(name) if on else off.add(name)
    _settings().update({"skills": {"disabled": ",".join(sorted(off))}})
    log.info("skill %s %s", name, "on" if on else "off")


def declarations() -> list[dict]:
    return [d for module in enabled().values() for d in getattr(module, "DECLARATIONS", [])]


def handler(tool: str):
    """(handler, None) for an enabled skill's tool, (None, reason) if switched off,
    (None, None) if no skill has this tool."""
    off = disabled()
    for name, module in available().items():
        if tool in module.HANDLERS:
            if name in off:
                return None, f"De skill {name} staat uit (dashboard, tab Skills)."
            return module.HANDLERS[tool], None
    return None, None


def info() -> list[dict]:
    """For the dashboard: every skill, on or off, with its tools and card."""
    off = disabled()
    rows = []
    for name, module in available().items():
        card = None
        if callable(getattr(module, "card", None)):
            try:
                card = module.card()
            except Exception as exc:
                card = {"fout": f"{type(exc).__name__}: {exc}"}
        rows.append({
            "name": name,
            "label": getattr(module, "LABEL", name),
            "enabled": name not in off,
            "tools": [d["name"] for d in getattr(module, "DECLARATIONS", [])],
            "card": card,
        })
    rows += [{"name": name, "label": name, "enabled": False, "tools": [], "card": None, "error": error}
             for name, error in sorted(_errors.items())]
    return rows
