import httpx
import pytest

from app.llm import chat_completion

_NOSLEEP = lambda _s: None


def _client(handler):
    return httpx.Client(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))


def test_retries_on_503_then_succeeds():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    out = chat_completion(_client(handler), "m", [{"role": "user", "content": "x"}], sleep=_NOSLEEP)
    assert out == "ok"
    assert calls["n"] == 2


def test_does_not_retry_on_4xx():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(httpx.HTTPStatusError):
        chat_completion(_client(handler), "m", [{"role": "user", "content": "x"}], sleep=_NOSLEEP)
    assert calls["n"] == 1


def test_raises_after_exhausting_retries():
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        chat_completion(_client(handler), "m", [{"role": "user", "content": "x"}], attempts=3, sleep=_NOSLEEP)
    assert calls["n"] == 3
