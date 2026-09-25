"""L2: core tools in full, the rest behind `doe`."""
import asyncio

from willie.voice import lean
from willie.voice.base import Tool


def _tools(log):
    def make(name, required=()):
        return Tool(name, f"{name} doet iets. Meer uitleg.",
                    {"type": "object", "properties": {r: {"type": "string"} for r in required},
                     "required": list(required)},
                    handler=lambda args, n=name: log.append((n, args)) or {"ok": n})
    return [make("toon", ["tekst"]), make("agenda"), make("printer_status"), make("zet_uit", ["actie"])]


def test_core_stays_rest_goes_behind_doe():
    tools = lean.lean(_tools([]), core=("toon",))
    assert [t.name for t in tools] == ["toon", "doe"]
    doe = tools[-1].description
    assert "- agenda(): agenda doet iets" in doe and "- zet_uit(actie):" in doe


def test_doe_runs_the_real_handler():
    log = []
    doe = lean.lean(_tools(log), core=("toon",))[-1]
    assert asyncio.run(doe.handler({"actie": "agenda", "argumenten": "{}"})) == {"ok": "agenda"}
    assert asyncio.run(doe.handler({"actie": "zet_uit", "argumenten": {"actie": "uit"}})) == {"ok": "zet_uit"}
    assert log == [("agenda", {}), ("zet_uit", {"actie": "uit"})]


def test_doe_explains_instead_of_guessing():
    log = []
    doe = lean.lean(_tools(log), core=("toon",))[-1]
    missing = asyncio.run(doe.handler({"actie": "zet_uit"}))
    assert "ontbreekt" in missing["fout"] and missing["argumenten"]["required"] == ["actie"]
    unknown = asyncio.run(doe.handler({"actie": "vlieg"}))
    assert "agenda" in unknown["acties"]
    bad = asyncio.run(doe.handler({"actie": "agenda", "argumenten": "{niet json"}))
    assert "JSON" in bad["fout"]
    assert log == []


def test_real_tool_list_shrinks():
    import json
    from willie.voice import gemini_live
    full = gemini_live.willie_tool_list(None, web_search=True)
    small = lean.lean(full)
    size = lambda ts: len(json.dumps([t.declaration() for t in ts], ensure_ascii=False))
    assert size(small) < size(full) / 2
    assert {"kijk", "stilstaan", "doe"} <= {t.name for t in small}
