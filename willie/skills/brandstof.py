"""Brandstof: Wouter's food, health and activity monitor, as a skill for WILL-E (21 Sep).

Brandstof runs on the MacBook (~/Documents/Brandstof) and serves a local JSON API; its
LAN instance is reachable from the Pi. Its own rule 2 carries over here: *the model does
not calculate*. Brandstof computes everything; this module only picks the computed
numbers and Brandstof's own sentences per topic, so the model can explain them. A day
payload is ~110 kB - far too much to hand a voice model - so each topic returns a
compact summary of 1-3 kB.

The Mac is optional hardware (D22): when it is asleep or off, the tool says so and the
conversation carries on.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date, timedelta

DEFAULT_URL = "http://Wouters-MacBook-Pro.local:8000"
TOPICS = ("overzicht", "voeding", "activiteit", "slaap", "training", "lichaam",
          "doelen", "tips", "lucht", "experimenten")


def base_url() -> str:
    try:
        from willie.config import Config
        return str(Config().get("skills.brandstof_url")).rstrip("/") or DEFAULT_URL
    except Exception:
        return DEFAULT_URL


def _get(path: str, timeout: float = 15.0):
    request = urllib.request.Request(f"{base_url()}/api/{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _day(datum: str) -> str:
    datum = (datum or "vandaag").strip().lower()
    if datum in ("vandaag", "today", ""):
        return date.today().isoformat()
    if datum in ("gisteren", "yesterday"):
        return (date.today() - timedelta(days=1)).isoformat()
    date.fromisoformat(datum)                      # raises ValueError on nonsense
    return datum


def _r(value, digits=0):
    if isinstance(value, (int, float)):
        return round(value, digits) if digits else round(value)
    return value


def _pick(obj: dict, *keys):
    return {k: obj.get(k) for k in keys if obj.get(k) not in (None, "", [], {})}


# ---- per topic ---------------------------------------------------------------------------
def _overview(d: dict) -> dict:
    score, status = d.get("score") or {}, d.get("status") or {}
    return {
        "dagscore": _pick(score, "score", "label", "text", "compare_text", "provisional"),
        "onderdelen": [_pick(p, "label", "score", "text") for p in score.get("parts", [])],
        "toestand": _pick(status, "label", "summary", "lever", "confidence_text"),
        "signalen": [f"{f.get('severity')}: {f.get('title')}" for f in d.get("flags", [])][:8],
        "tijd": (d.get("daypart") or {}).get("time"),
    }


def _food(d: dict) -> dict:
    energy, nutrition = d.get("energy") or {}, d.get("nutrition") or {}
    macros = {k: _r((v or {}).get("amount")) for k, v in (d.get("macros") or {}).items()}
    window = d.get("micronutrients_window") or {}
    return {
        "energie": {"gegeten_kcal": _r(energy.get("intake_kcal")), "verbruik_kcal": _r(energy.get("tdee")),
                    "verbruikt_tot_nu": _r((energy.get("spend") or {}).get("so_far")),
                    "balans_7d_gem": _r(energy.get("rolling_balance_7d"))},
        "macros_gegeten": macros,
        "doelen": _pick(d.get("targets") or {}, "kcal_target", "protein_g", "carb_g", "fat_g", "day_type"),
        "tempo": [p.get("text") for p in (d.get("pace") or {}).get("paces", [])],
        "tempo_verhaal": (d.get("pace") or {}).get("story"),
        "voeding_score": _pick(nutrition, "score", "verdict"),
        "vandaag": (nutrition.get("day") or {}).get("story"),
        "drinken": _pick(d.get("hydration") or {}, "headline", "detail", "total_ml", "target_ml"),
        "micro_28d": {"samenvatting": window.get("summary"), "zorgen": window.get("concerns")},
        "signalen": [f.get("title") for f in d.get("flags", []) if f.get("severity") in ("act", "watch")],
        "toestand": _pick(d.get("status") or {}, "label", "lever"),
    }


def _activity(d: dict) -> dict:
    movement, week = d.get("movement") or {}, d.get("week") or {}
    return {
        "beweging": {**_pick(movement, "steps", "median_steps_28d", "intensity_minutes", "vigorous_minutes",
                             "moderate_minutes", "sedentary_hours"),
                     "tempo": (movement.get("pace") or {}).get("text")},
        "activiteiten": [{"sport": a.get("sport"), "naam": a.get("name"), "km": _r((a.get("distance_m") or 0) / 1000, 1),
                          "minuten": _r((a.get("duration_s") or 0) / 60), "kcal": _r(a.get("kcal")),
                          "hartslag_gem": a.get("avg_hr"), "belasting": _r(a.get("training_load"))}
                         for a in d.get("activities", [])],
        "dagverhaal": ((d.get("load") or {}).get("story") or {}).get("headline"),
        "week": {**_pick(week, "intensity_minutes", "mean_steps", "strength_days", "meets_mvpa", "meets_strength"),
                 "checks": [f"{c.get('label')}: {c.get('value')} {c.get('unit')} (doel {c.get('target_text')})"
                            for c in week.get("checks", [])]},
        "trends": {k: {"nu": v.get("latest"), "gem": v.get("mean"), "richting": v.get("direction")}
                   for k, v in (d.get("sparks") or {}).items()
                   if k in ("steps", "intensity_minutes", "sedentary_minutes", "floors_climbed", "load")},
    }


def _sleep(d: dict) -> dict:
    recovery, timing = d.get("recovery_trend") or {}, d.get("sleep_timing") or {}
    parts = {p.get("key"): p for p in (d.get("score") or {}).get("parts", [])}
    return {
        "slaap": _pick(parts.get("sleep") or {}, "score", "text", "detail"),
        "herstel": {**_pick(recovery, "label", "detail"),
                    "onderdelen": [f"{p.get('label')}: {p.get('detail')}" for p in recovery.get("parts", [])]},
        "ritme": timing.get("story", [])[:3],
        "trends": {k: {"nu": v.get("latest"), "gem": v.get("mean"), "eenheid": v.get("unit"), "richting": v.get("direction")}
                   for k, v in (d.get("sparks") or {}).items()
                   if k in ("sleep_minutes", "sleep_score", "hrv", "resting_hr", "body_battery_charged", "sleep_regularity")},
    }


def _training(d: dict) -> dict:
    t = d.get("training") or {}
    out = {"week": _pick(t, "goal", "phase_label", "run_days", "strength_days", "total_minutes", "total_km",
                         "change_pct", "story"),
           "volgende": t.get("next"),
           "dagen": [f"{x.get('weekday_short')}: {x.get('kind')}{' ' + str(x['minutes']) + ' min' if x.get('minutes') else ''}"
                     for x in t.get("days", [])]}
    try:
        full = _get("training", timeout=20)
        fitness = full.get("fitness") or {}
        out["conditie"] = {"vdot": fitness.get("vdot"),
                           "voorspelde_tijden": [f"{e.get('label')}: {e.get('time_text')}" for e in fitness.get("equivalents", [])]}
        out["advies_volume"] = (full.get("volume_advice") or {}).get("note")
        out["recente_loopjes"] = [f"{r.get('date')}: {r.get('km')} km in {_r(r.get('minutes'))} min, {r.get('pace_text')}/km"
                                  for r in (full.get("history") or {}).get("runs", [])[:5]]
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return out


def _body(d: dict) -> dict:
    body = d.get("body") or {}
    return {"laatste": body.get("latest"), "trend": body.get("trend"), "opmerking": body.get("note"),
            "geschiedenis": body.get("history", [])[-5:]}


def _goals(datum: str) -> dict:
    g = _get("goals/daily")
    return {"doelen": [f"{x.get('name')} ({x.get('what')}): {x.get('value')} {x.get('unit') or ''}"
                       f" - {x.get('tier_label') or 'nog geen medaille'}; {x.get('text') or ''}".strip()
                       for x in g.get("goals", []) if x.get("challenge") in g.get("chosen", [])]}


def _tips(datum: str) -> dict:
    s = _get("suggestions", timeout=30)
    return {"tips": [{"tekort": (t.get("gap") or {}).get("headline"),
                      "idee": (t.get("recipe") or {}).get("name"),
                      "maaltijd": (t.get("recipe") or {}).get("meal")} for t in s.get("tips", [])[:5]]}


def _air(datum: str) -> dict:
    a = _get("air")
    return {"binnen": _pick(a.get("indoor") or {}, "level_label", "co2", "co2_peak", "pm2_5", "temperature", "humidity", "voc"),
            "buiten": _pick(a.get("outdoor") or {}, "level_label", "aqi", "temperature"),
            "advies": a.get("advice"), "verhaal": a.get("story")}


def _experiments(datum: str) -> dict:
    e = _get("experiments/home")
    return {"lopend": [f"{x.get('name')}: {x.get('change_to')} - dag {x.get('elapsed')} van {x.get('days')}, "
                       f"{x.get('kept_pct')}% volgehouden" for x in e.get("active", [])]}


DAY_TOPICS = {"overzicht": _overview, "voeding": _food, "activiteit": _activity, "slaap": _sleep,
              "training": _training, "lichaam": _body}
OTHER_TOPICS = {"doelen": _goals, "tips": _tips, "lucht": _air, "experimenten": _experiments}


def gezondheid(onderwerp: str = "overzicht", datum: str = "vandaag") -> dict:
    onderwerp = (onderwerp or "overzicht").strip().lower()
    if onderwerp not in TOPICS:
        return {"fout": f"onbekend onderwerp; kies uit {', '.join(TOPICS)}"}
    try:
        day = _day(datum)
    except ValueError:
        return {"fout": "datum als vandaag, gisteren of JJJJ-MM-DD"}
    try:
        if onderwerp in DAY_TOPICS:
            result = DAY_TOPICS[onderwerp](_get(f"day/{day}", timeout=20))
        else:
            result = OTHER_TOPICS[onderwerp](day)
    except urllib.error.HTTPError as exc:
        return {"fout": f"Brandstof gaf fout {exc.code}"}
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"fout": "Brandstof is niet bereikbaar - staat de MacBook aan en draait Brandstof?"}
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > 4000:                              # keep the voice model's context small
        result = {"samenvatting": text[:4000]}
    return {"bron": "Brandstof", "datum": day, "onderwerp": onderwerp, **result}


DECLARATION = {
    "name": "gezondheid",
    "description": (
        "Haal Wouters gezondheids-, voedings- en activiteitsgegevens uit Brandstof (zijn eigen "
        "monitor op de MacBook, met Garmin en Mijn Eetmeter). Gebruik dit bij vragen als: hoe sta "
        "ik ervoor, wat heb ik gegeten, hoeveel eiwit nog, hoe was mijn slaap, hoe actief ben ik, "
        "wat is mijn training, mijn gewicht, mijn doelen, voedingstips, de luchtkwaliteit binnen. "
        "Brandstof rekent alles uit; jij legt het uit en rekent zelf niets na."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "onderwerp": {"type": "string", "enum": list(TOPICS),
                          "description": "overzicht = dagscore + toestand; voeding = eten, macro's, drinken, "
                                         "tekorten; activiteit = stappen, sport, week; slaap = slaap + herstel; "
                                         "training = schema, conditie; lichaam = gewicht, vet; doelen = medailles; "
                                         "tips = voedingstips; lucht = luchtkwaliteit binnen/buiten; "
                                         "experimenten = lopende experimenten"},
            "datum": {"type": "string", "description": "vandaag (standaard), gisteren of JJJJ-MM-DD"},
        },
        "required": ["onderwerp"],
    },
}
