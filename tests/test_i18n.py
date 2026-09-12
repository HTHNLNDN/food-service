from app.db import bootstrap, connect
from app.i18n import UI_STRINGS, cached, warm


class FakeBatchTranslator:
    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = []

    def translate_batch(self, texts, language):
        self.calls.append((texts, language))
        if self.raises:
            raise self.raises
        return self.result


def test_cached_is_empty_for_blank_language(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    assert cached(conn, "") == {}


def test_warm_populates_cache_from_translator(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    translated = [f"[{s}]" for s in UI_STRINGS]  # stand-in translation, same order/count
    translator = FakeBatchTranslator(result=translated)
    warm(conn, "Danish", translator)
    result = cached(conn, "Danish")
    assert result["This week"] == "[This week]"
    assert len(result) == len(UI_STRINGS)
    assert translator.calls[0][1] == "Danish"


def test_warm_is_a_noop_for_blank_language(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    translator = FakeBatchTranslator(result=[f"[{s}]" for s in UI_STRINGS])
    warm(conn, "", translator)
    assert translator.calls == []
    assert cached(conn, "Danish") == {}


def test_warm_does_not_re_translate_already_cached_strings(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    translator = FakeBatchTranslator(result=[f"[{s}]" for s in UI_STRINGS])
    warm(conn, "Danish", translator)
    warm(conn, "Danish", translator)  # nothing left to translate
    assert len(translator.calls) == 1


def test_warm_leaves_cache_untouched_on_count_mismatch(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    translator = FakeBatchTranslator(result=["only one"])
    warm(conn, "Danish", translator)
    assert cached(conn, "Danish") == {}


def test_warm_leaves_cache_untouched_when_translator_raises(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    translator = FakeBatchTranslator(raises=RuntimeError("network blip"))
    warm(conn, "Danish", translator)
    assert cached(conn, "Danish") == {}


def test_warm_is_a_noop_without_a_translator(tmp_path):
    conn = connect(tmp_path / "e.db")
    bootstrap(conn)
    warm(conn, "Danish", None)
    assert cached(conn, "Danish") == {}
