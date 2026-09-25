"""J3 token meter: usageMetadata -> counts -> USD."""
from willie import usage


def test_tokens_split_by_modality():
    meta = {"promptTokenCount": 100, "responseTokenCount": 30, "thoughtsTokenCount": 5,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 60}, {"modality": "AUDIO", "tokenCount": 30},
                                    {"modality": "IMAGE", "tokenCount": 10}],
            "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 30}]}
    t = usage.tokens(meta)
    assert t == {"prompt_text": 60, "prompt_audio": 30, "prompt_image": 10,
                 "out_text": 0, "out_audio": 30, "thoughts": 5}


def test_totals_only_count_as_text():
    t = usage.tokens({"promptTokenCount": 50, "candidatesTokenCount": 20})
    assert (t["prompt_text"], t["out_text"]) == (50, 20)


def test_add_sums_turns():
    total = {}
    for _ in range(3):
        usage.add(total, {"promptTokenCount": 4000, "responseTokenCount": 20})
    assert total["turns"] == 3 and total["prompt_text"] == 12000


def test_usd_live_prices():
    # 1M text in + 1M audio out on a Live model = $0.50 + $12.
    assert round(usage.usd("gemini-3.8-live", {"prompt_text": 1_000_000, "out_audio": 1_000_000}), 2) == 12.5
    assert usage.family("gemini-flash-lite-latest") == "flash-lite"
    assert usage.family("gemini-2.5-flash-preview-tts") == "tts"


def test_report_never_raises(monkeypatch):
    def broken(record):
        raise RuntimeError("hub gone")
    monkeypatch.setattr(usage, "HOOK", broken)
    usage.report("robot", "models/gemini-3.8-live", "gratis", meta={"promptTokenCount": 1})
    got = []
    monkeypatch.setattr(usage, "HOOK", got.append)
    usage.report("robot", "models/x", "gratis", counts={})       # empty session: nothing sent
    assert got == []
