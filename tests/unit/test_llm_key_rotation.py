"""Several keys for one LLM endpoint: a rate-limited key hands over to the next.

A free tier (Groq) limits each key per minute.  The client must try the next
configured key on a 429 at once rather than sleep while another key is idle,
and only fall back to its backoff when every key is rate-limited.  No network:
``httpx.Client`` is replaced by a stub that answers per key.
"""

from __future__ import annotations

from typing import Any

import pytest
from dreamjob.config import Settings
from test_llm_call_log import _Response, db  # noqa: F401 - the db fixture, used by name

ANSWER = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


class _Limited(_Response):
    def __init__(self) -> None:
        super().__init__({})
        self.status_code = 429

    def raise_for_status(self) -> None:
        raise RuntimeError("429 Too Many Requests")


def _per_key(limited: set[str], seen: list[str]):
    """An ``httpx.Client`` stand-in answering 429 for the keys in ``limited``."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            key = headers["Authorization"].removeprefix("Bearer ")
            seen.append(key)
            return _Limited() if key in limited else _Response(ANSWER)

    return _Client


def _client(**keys: str):
    from dreamjob.llm.client import LLMClient

    client = LLMClient(job_seeker_id=None)
    client.settings = client.settings.model_copy(update={"deepseek_api_key": "k1", **keys})
    return client


def test_keys_are_listed_in_order_without_blanks_or_repeats() -> None:
    settings = Settings().model_copy(update={
        "deepseek_api_key": "k1", "deepseek_api_key_2": " ", "deepseek_api_key_3": "k3",
        "deepseek_api_key_4": "k1", "deepseek_api_key_5": "k5",
    })
    assert settings.deepseek_api_keys == ["k1", "k3", "k5"]


@pytest.mark.usefixtures("db")
def test_a_rate_limited_key_hands_over_to_the_next_without_sleeping(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod

    seen: list[str] = []
    slept: list[float] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _per_key({"k1"}, seen))
    monkeypatch.setattr(client_mod.time, "sleep", slept.append)

    result = _client(deepseek_api_key_2="k2").complete("classify.reply", "s", "u", retries=2)

    assert result.text == "ok"
    assert seen == ["k1", "k2"]
    assert slept == []


@pytest.mark.usefixtures("db")
def test_every_key_rate_limited_fails_after_trying_each_once(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import LLMError

    seen: list[str] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _per_key({"k1", "k2"}, seen))
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    with pytest.raises(LLMError):
        _client(deepseek_api_key_2="k2").complete("classify.reply", "s", "u", retries=0)

    assert seen == ["k1", "k2"]


@pytest.mark.usefixtures("db")
def test_the_fallback_endpoint_answers_when_every_main_key_fails(monkeypatch) -> None:
    """Groq first; DeepSeek only once every Groq key is rate-limited."""
    import dreamjob.llm.client as client_mod

    seen: list[str] = []
    urls: list[str] = []
    models: list[str] = []
    limited = _per_key({"k1", "k2"}, seen)

    class _Recording(limited):
        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            urls.append(url)
            models.append(json["model"])
            return super().post(url, headers=headers, json=json)

    monkeypatch.setattr(client_mod.httpx, "Client", _Recording)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    client = _client(
        deepseek_api_key_2="k2",
        deepseek_base_url="https://api.groq.com/openai/v1",
        llm_model_cheap="llama-3.1-8b-instant",
        fallback_llm_base_url="https://api.deepseek.com",
        fallback_llm_api_key="ds",
        fallback_llm_model_cheap="deepseek-chat",
    )

    result = client.complete("classify.reply", "s", "u", prefer_strong=False, retries=0)

    assert result.text == "ok"
    assert seen == ["k1", "k2", "ds"]
    assert urls[-1] == "https://api.deepseek.com/chat/completions"
    assert models == ["llama-3.1-8b-instant", "llama-3.1-8b-instant", "deepseek-chat"]
    assert result.model == "deepseek-chat"


@pytest.mark.usefixtures("db")
def test_no_fallback_key_means_no_fallback_call(monkeypatch) -> None:
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import LLMError

    seen: list[str] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _per_key({"k1"}, seen))
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    with pytest.raises(LLMError):
        _client(fallback_llm_api_key="").complete("classify.reply", "s", "u", retries=0)

    assert seen == ["k1"]
