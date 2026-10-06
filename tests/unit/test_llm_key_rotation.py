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

# `Settings` reads the real .env, so every key field has to be blanked before
# the overrides are applied.  Without this baseline a configured key takes part
# in the test: the rotation assertions then depend on how many keys the machine
# happens to have, and a failing assertion prints a live key into the pytest
# output.
_BLANK_KEYS = {
    "deepseek_api_key": "",
    "deepseek_api_key_2": "",
    "deepseek_api_key_3": "",
    "deepseek_api_key_4": "",
    "deepseek_api_key_5": "",
    "fallback_llm_api_key": "",
}


def _settings(**overrides: object):
    """Settings with every API key blanked, then `overrides` applied."""
    return Settings().model_copy(update={**_BLANK_KEYS, **overrides})


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
    client.settings = client.settings.model_copy(
        update={**_BLANK_KEYS, "deepseek_api_key": "k1", **keys}
    )
    return client


def test_keys_are_listed_in_order_without_blanks_or_repeats() -> None:
    settings = _settings(
        deepseek_api_key="k1", deepseek_api_key_2=" ", deepseek_api_key_3="k3",
        deepseek_api_key_4="k1", deepseek_api_key_5="k5",
    )
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


def _per_key_status(status: int, failing: set[str], seen: list[str]):
    """An ``httpx.Client`` stand-in answering `status` for the keys in `failing`."""

    class _Failed(_Response):
        def __init__(self) -> None:
            super().__init__({})
            self.status_code = status

        def raise_for_status(self) -> None:
            raise RuntimeError(f"{status}")

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            key = headers["Authorization"].removeprefix("Bearer ")
            seen.append(key)
            return _Failed() if key in failing else _Response(ANSWER)

    return _Client


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.usefixtures("db")
def test_a_revoked_first_key_hands_over_to_the_next_and_not_to_the_paid_endpoint(
    monkeypatch, status: int
) -> None:
    """A dead key must not route the whole call to the paid fallback.

    The rotation used to end on any status other than 429, so one revoked or
    mistyped first key skipped keys 2..5 and every call fell through to the
    paid endpoint instead - billed, and logged at the free rate.
    """
    import dreamjob.llm.client as client_mod

    seen: list[str] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _per_key_status(status, {"k1"}, seen))
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    result = _client(
        deepseek_api_key_2="k2",
        fallback_llm_base_url="https://api.deepseek.com",
        fallback_llm_api_key="PAID",
    ).complete("classify.reply", "s", "u", retries=2)

    assert result.text == "ok"
    assert seen == ["k1", "k2"]
    assert "PAID" not in seen
    assert result.provider != "fallback"


@pytest.mark.parametrize("status", [400, 404])
@pytest.mark.usefixtures("db")
def test_a_malformed_request_does_not_burn_every_key(monkeypatch, status: int) -> None:
    """A status about the request, not the key: another key answers it the same."""
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import LLMError

    seen: list[str] = []
    monkeypatch.setattr(
        client_mod.httpx, "Client", _per_key_status(status, {"k1", "k2"}, seen)
    )
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    with pytest.raises(LLMError):
        _client(deepseek_api_key_2="k2").complete("classify.reply", "s", "u", retries=0)

    assert seen == ["k1"]


@pytest.mark.usefixtures("db")
def test_a_fallback_answer_is_priced_at_the_fallback_rates(monkeypatch) -> None:
    """The paid endpoint is never debited at the free endpoint's rates.

    The main endpoint is a free tier, so its rates are legitimately 0.  One
    shared rate pair meant every paid fallback call was recorded at 0.00 EUR.
    """
    import dreamjob.llm.client as client_mod

    seen: list[str] = []
    monkeypatch.setattr(client_mod.httpx, "Client", _per_key({"k1"}, seen))
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(client_mod, "admin_config", lambda: {})
    client = _client(
        deepseek_api_key="k1",
        llm_cost_per_1m_input_eur=0.0,
        llm_cost_per_1m_output_eur=0.0,
        fallback_llm_base_url="https://api.deepseek.com",
        fallback_llm_api_key="PAID",
        fallback_llm_cost_per_1m_input_eur=0.25,
        fallback_llm_cost_per_1m_output_eur=1.00,
    )

    # The free endpoint prices a call at zero, as configured.
    assert client._price(1_000_000, 1_000_000, "groq") == 0.0
    # The same call on the paid endpoint is not free.
    assert client._price(1_000_000, 1_000_000, "fallback") == pytest.approx(1.25)


@pytest.mark.usefixtures("db")
def test_every_configured_key_is_tried_before_the_paid_endpoint(monkeypatch) -> None:
    """Five keys, the first four dead: the fifth answers and nothing is billed.

    The rotation used to stop on the first non-429, so one revoked key meant
    keys 2..5 were never reached.  Two keys is not enough to pin that: the loop
    has to be shown walking the whole list.
    """
    import dreamjob.llm.client as client_mod

    seen: list[str] = []
    monkeypatch.setattr(
        client_mod.httpx, "Client",
        _per_key_status(401, {"k1", "k2", "k3", "k4"}, seen),
    )
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    result = _client(
        deepseek_api_key_2="k2", deepseek_api_key_3="k3",
        deepseek_api_key_4="k4", deepseek_api_key_5="k5",
        fallback_llm_base_url="https://api.deepseek.com",
        fallback_llm_api_key="PAID",
    ).complete("classify.reply", "s", "u", retries=0)

    assert result.text == "ok"
    assert seen == ["k1", "k2", "k3", "k4", "k5"]
    assert "PAID" not in seen
    assert result.provider != "fallback"
    assert result.usage.cost_eur == 0.0, "the free endpoint answered, so nothing is billed"


@pytest.mark.usefixtures("db")
def test_a_mix_of_429_and_401_still_walks_the_whole_list(monkeypatch) -> None:
    """The two key-specific statuses are not handled on separate code paths."""
    import dreamjob.llm.client as client_mod

    seen: list[str] = []

    class _Mixed(_Response):
        def __init__(self, status: int) -> None:
            super().__init__({})
            self.status_code = status

        def raise_for_status(self) -> None:
            raise RuntimeError(str(self.status_code))

    statuses = {"k1": 429, "k2": 401, "k3": 403}

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, *, headers: dict, json: dict) -> _Response:
            key = headers["Authorization"].removeprefix("Bearer ")
            seen.append(key)
            return _Mixed(statuses[key]) if key in statuses else _Response(ANSWER)

    monkeypatch.setattr(client_mod.httpx, "Client", _Client)
    monkeypatch.setattr(client_mod.time, "sleep", lambda _s: None)

    result = _client(
        deepseek_api_key_2="k2", deepseek_api_key_3="k3", deepseek_api_key_4="k4",
    ).complete("classify.reply", "s", "u", retries=0)

    assert result.text == "ok"
    assert seen == ["k1", "k2", "k3", "k4"]
