"""L2 small prompt: a few core tools in full, the rest behind one `doe` tool (25 Sep, Wouter).

The Live API pays for the whole setup again on every turn (§12: 15.6k text tokens, of which
~27 KB is 62 tool declarations). Most tools are used rarely, so their full JSON schema does
not need to be in every turn. `doe` carries a one-line catalogue (name, arguments, what it
does); the model names the action and passes the arguments as JSON. A wrong or missing
argument comes back as an error with the full description and schema, so the model can
correct itself in the same turn.

Safety does not depend on the descriptions: confirmations (power off, code changes, printer
control) are enforced in the handlers, which run exactly as before.
"""
from __future__ import annotations

import inspect
import json

from willie.voice.base import Tool

# Always in full: used in most conversations, or must be instant (stop), or need their
# rules on every turn. The runner adds niet_voor_mij on top.
CORE = ("toon", "kijk", "onthoud", "herinner", "stilstaan", "rijden", "draaien",
        "muziek_bediening", "zoek_op", "huis_nu")

# Garage mode (K1): the workshop tools are core while it is on.
GARAGE_CORE = ("pinout", "pinout_zoom", "pinout_weg", "stappenplan", "voertuig", "voertuig_log",
               "boodschappen")

DOE_TEXT = (
    "Voer een van je andere vaardigheden uit. Kies de actie uit de lijst hieronder en geef de "
    "argumenten als JSON. Weet je de argumenten niet zeker, roep het dan toch aan: je krijgt "
    "dan de volledige uitleg terug. Gebruik dit zonder aarzelen; het is net zo goed als een "
    "gewone tool.\n\nActies:\n"
)
LINE_CHARS = 80


def signature(tool: Tool) -> str:
    props = (tool.parameters or {}).get("properties") or {}
    required = set((tool.parameters or {}).get("required") or ())
    args = []
    for name, spec in props.items():
        enum = spec.get("enum")
        shown = f"{name}={'|'.join(map(str, enum))}" if enum and len(enum) <= 5 else name
        args.append(shown if name in required else shown + "?")
    return f"{tool.name}({', '.join(args)})"


def summary(text: str) -> str:
    first = text.split(". ")[0].split("\n")[0].rstrip(".")
    return first if len(first) <= LINE_CHARS else first[:LINE_CHARS - 1].rstrip() + "…"


def catalogue(tools: list[Tool]) -> str:
    return "\n".join(f"- {signature(t)}: {summary(t.description)}" for t in tools)


def _parse(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def lean(tools: list[Tool], core: tuple[str, ...] = CORE) -> list[Tool]:
    """Core tools unchanged + one `doe` that runs any of the others."""
    keep = [t for t in tools if t.name in core]
    rest = {t.name: t for t in tools if t.name not in core}
    if not rest:
        return keep

    def explain(tool: Tool, why: str) -> dict:
        return {"fout": why, "actie": tool.name, "uitleg": tool.description, "argumenten": tool.parameters}

    async def run(args: dict) -> dict:
        name = str(args.get("actie", "")).strip()
        tool = rest.get(name)
        if tool is None:
            return {"fout": f"onbekende actie {name!r}", "acties": sorted(rest)}
        arguments = _parse(args.get("argumenten"))
        if arguments is None:
            return explain(tool, "argumenten is geen JSON-object")
        missing = [r for r in (tool.parameters or {}).get("required") or () if r not in arguments]
        if missing:
            return explain(tool, f"argument ontbreekt: {', '.join(missing)}")
        if tool.handler is None:
            return {"fout": f"{name} heeft geen uitvoering"}
        result = tool.handler(arguments)
        return await result if inspect.isawaitable(result) else result

    doe = Tool(
        "doe",
        DOE_TEXT + catalogue(list(rest.values())),
        {"type": "object", "properties": {
            "actie": {"type": "string", "description": "Naam uit de lijst, bijvoorbeeld agenda."},
            "argumenten": {"type": "string", "description": "JSON-object, bijvoorbeeld {\"dagen\": 3}; {} als er geen zijn."}},
         "required": ["actie"]},
        handler=run,
    )
    return [*keep, doe]
