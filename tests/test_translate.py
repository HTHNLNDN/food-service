import json

import httpx

from app.translate import LLMTranslator

BASE = "https://llm.test/v1"


def _completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _translator(handler):
    client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(handler))
    return LLMTranslator(BASE, "m", "k", client=client)


def test_translates_title_steps_and_ingredients():
    body = json.dumps({
        "title": "Kyllingegryde",
        "steps": ["Steg kyllingen.", "Server varmt."],
        "ingredients": ["kyllingebryst", "broccoli"],
    })
    t = _translator(lambda _r: _completion(body))
    result = t.translate("Chicken pot", ["Fry the chicken.", "Serve hot."],
                         ["chicken breast", "broccoli"], "Danish")
    assert result.title == "Kyllingegryde"
    assert result.steps == ["Steg kyllingen.", "Server varmt."]
    assert result.ingredient_names == ["kyllingebryst", "broccoli"]


def test_tolerates_fenced_and_prose_json():
    body = "Sure, here you go:\n```json\n" + json.dumps({
        "title": "T", "steps": ["s"], "ingredients": ["i"],
    }) + "\n```"
    t = _translator(lambda _r: _completion(body))
    result = t.translate("T", ["s"], ["i"], "French")
    assert result.title == "T"


def test_returns_none_on_step_count_mismatch():
    body = json.dumps({"title": "T", "steps": ["only one"], "ingredients": ["i"]})
    t = _translator(lambda _r: _completion(body))
    assert t.translate("T", ["a", "b"], ["i"], "German") is None


def test_returns_none_on_ingredient_count_mismatch():
    body = json.dumps({"title": "T", "steps": ["s"], "ingredients": ["only", "one", "extra"]})
    t = _translator(lambda _r: _completion(body))
    assert t.translate("T", ["s"], ["i"], "German") is None


def test_returns_none_on_malformed_json():
    t = _translator(lambda _r: _completion("not json at all"))
    assert t.translate("T", ["s"], ["i"], "German") is None


def test_returns_none_when_title_is_not_a_string():
    body = json.dumps({"title": None, "steps": ["s"], "ingredients": ["i"]})
    t = _translator(lambda _r: _completion(body))
    assert t.translate("T", ["s"], ["i"], "German") is None


# --- translate_batch: flat-list UI-string translation (app/i18n.py's warm cache) ---


def test_translate_batch_translates_each_string_in_order():
    body = json.dumps({"strings": ["Denne uge", "Indkøb"]})
    t = _translator(lambda _r: _completion(body))
    assert t.translate_batch(["This week", "Shopping"], "Danish") == ["Denne uge", "Indkøb"]


def test_translate_batch_returns_none_on_count_mismatch():
    body = json.dumps({"strings": ["only one"]})
    t = _translator(lambda _r: _completion(body))
    assert t.translate_batch(["This week", "Shopping"], "Danish") is None


def test_translate_batch_returns_none_on_malformed_json():
    t = _translator(lambda _r: _completion("not json at all"))
    assert t.translate_batch(["This week"], "Danish") is None
