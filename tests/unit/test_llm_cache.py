"""Model routing for generation, and the response cache (FR-363, NFR-104).

Two findings from E2E_1500 section 8.9 are pinned here: the generation tasks are
prose writing and belong on the chat model, and a repeat extraction of the same
page must not be paid for twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.db import connection as conn_mod
from dreamjob.db.migrator import migrate
from dreamjob.llm import client as llm


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "llm_cache.db"
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))
    llm.invalidate_admin_config()
    yield path
    llm.invalidate_admin_config()


def test_generation_tasks_route_to_the_chat_model(db):
    """FR-363: the reasoning model returned nothing for over half of them."""
    client = llm.LLMClient()
    for task in ("generate.cv", "generate.email", "generate.motivation", "generate.briefing"):
        _, _, model, _ = client.route(task)
        assert model == client.settings.llm_model_cheap, task


def test_reasoning_tasks_still_route_to_the_strong_model(db):
    client = llm.LLMClient()
    for task in ("profile.composite", "score.opportunity", "analysis.financial"):
        _, _, model, _ = client.route(task)
        assert model == client.settings.llm_model_strong, task


def test_a_repeat_extraction_is_served_from_cache_without_a_second_call(db, monkeypatch):
    client = llm.LLMClient()
    monkeypatch.setattr(
        client, "route", lambda task, prefer_strong=None: ("http://x/v1", "k", "m", "deepseek")
    )
    logged: list = []
    monkeypatch.setattr(client, "_log_call", lambda *a, **k: logged.append(k.get("status")))

    network_calls: list[int] = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [
                    {"message": {"content": '{"title": "Data Engineer"}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, *args, **kwargs):
            network_calls.append(1)
            return FakeResponse()

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)

    first = client.complete("extract.vacancy", "sys", "user", json_mode=True)
    second = client.complete("extract.vacancy", "sys", "user", json_mode=True)

    assert first.text == second.text
    assert len(network_calls) == 1, "the second identical extraction must not hit the network"
    assert "cached" in logged


def test_a_generation_answer_is_never_cached(db, monkeypatch):
    """A cached CV would be a stale CV; generation is asked for once."""
    assert "generate.cv" not in llm.CACHEABLE_TASKS
    assert "extract.vacancy" in llm.CACHEABLE_TASKS


# ---------------------------------------------------------------------------
# What the cache replays: the model that ANSWERED, not the one that was asked
# ---------------------------------------------------------------------------


def _fallback_client(monkeypatch):
    """A client whose main endpoint refuses every key and whose fallback answers.

    Every credential is written here rather than inherited: ``Settings`` reads
    the real ``.env``, where the main endpoint is a free tier with its rates at
    0, and a test about a paid call must not depend on that file.
    """
    answer = {
        "choices": [{"message": {"content": '{"title": "Data Engineer"}'},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
    }
    posted: list[str] = []

    class _Resp:
        def __init__(self, status: int, payload: dict) -> None:
            self.status_code = status
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(str(self.status_code))

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> bool:
            return False

        def post(self, url, *, headers, json):
            key = headers["Authorization"].removeprefix("Bearer ")
            posted.append(key)
            return _Resp(200, answer) if key == "PAID" else _Resp(429, {})

    monkeypatch.setattr(llm.httpx, "Client", _Client)
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)
    monkeypatch.setattr(llm, "admin_config", lambda: {})

    client = llm.LLMClient(job_seeker_id=None)
    client.settings = client.settings.model_copy(update={
        "deepseek_api_key": "free1", "deepseek_api_key_2": "free2",
        "deepseek_api_key_3": "", "deepseek_api_key_4": "", "deepseek_api_key_5": "",
        "deepseek_base_url": "https://free.test/v1",
        "llm_cost_per_1m_input_eur": 0.0, "llm_cost_per_1m_output_eur": 0.0,
        "fallback_llm_base_url": "https://paid.test", "fallback_llm_api_key": "PAID",
        "fallback_llm_cost_per_1m_input_eur": 0.25,
        "fallback_llm_cost_per_1m_output_eur": 1.00,
    })
    return client, posted


def test_a_cache_hit_on_a_fallback_answer_is_not_logged_as_a_free_model_call(db, monkeypatch):
    """The cache key is built from the model that was ASKED.

    A call that failed over to the paid endpoint is stored under the free
    model's key, so replaying only the text logged every later hit as a call to
    the free model - the paid answer appeared once in the FR-364 log and then
    disappeared behind the free model's name for as long as it was cached.
    """
    client, posted = _fallback_client(monkeypatch)
    logged: list[tuple] = []
    real_log = client._log_call
    monkeypatch.setattr(
        client, "_log_call",
        lambda *a, **k: logged.append((a[4], a[5], k.get("status"))) or real_log(*a, **k),
    )

    first = client.complete("extract.vacancy", "sys", "user", json_mode=True, retries=0)
    second = client.complete("extract.vacancy", "sys", "user", json_mode=True, retries=0)

    assert first.provider == "fallback"
    assert first.usage.cost_eur == pytest.approx(1.25), "a paid call is never 0.00 EUR"
    assert posted == ["free1", "free2", "PAID"], posted
    # The second call never left the process, and still names the paid endpoint.
    assert second.provider == "fallback" and second.model == first.model
    assert logged[-1][2] == "cached"
    assert logged[-1][1] == "fallback", f"the cache hit was logged as {logged[-1][1]!r}"
    assert logged[-1][0] == first.model


def test_the_paid_fallback_warns_once_per_answered_call_and_never_on_a_cache_hit(
    db, monkeypatch, caplog
):
    """A billable event with no other trace has to be visible in the log.

    Exactly once per call the paid endpoint answers: not again on the cache hit
    (no call, no charge), and never when the free endpoint answered.
    """
    client, _posted = _fallback_client(monkeypatch)

    with caplog.at_level("WARNING", logger="dreamjob.llm.client"):
        client.complete("extract.vacancy", "sys", "user", json_mode=True, retries=0)
        paid = [r for r in caplog.records if "paid fallback endpoint" in r.getMessage()]
        assert len(paid) == 1, [r.getMessage() for r in caplog.records]

        caplog.clear()
        client.complete("extract.vacancy", "sys", "user", json_mode=True, retries=0)
        assert not [r for r in caplog.records if "paid fallback endpoint" in r.getMessage()]

    # The free endpoint answering warns about nothing.
    llm.clear_response_cache()
    client.settings = client.settings.model_copy(update={"deepseek_api_key": "PAID"})
    with caplog.at_level("WARNING", logger="dreamjob.llm.client"):
        caplog.clear()
        result = client.complete("extract.vacancy", "sys", "user", json_mode=True, retries=0)
        assert result.provider != "fallback"
        assert result.usage.cost_eur == 0.0, "the free endpoint's own rates still apply"
        assert not [r for r in caplog.records if "paid fallback endpoint" in r.getMessage()]


def test_the_response_cache_survives_concurrent_writers(db):
    """``_RESPONSE_CACHE`` is a process-global OrderedDict behind one lock.

    The entries are 4-tuples now, and an LRU eviction runs inside the same
    critical section, so a torn read would be a 2-tuple or a KeyError rather
    than a wrong number - which is why the shape is what is asserted.
    """
    import threading

    llm.clear_response_cache()
    errors: list[BaseException] = []

    def hammer(n: int) -> None:
        try:
            for i in range(200):
                key = f"k{(n * 200 + i) % 64}"
                llm._response_cache_put(key, f"t{i}", None, "m", "fallback")
                got = llm._response_cache_get(key)
                if got is not None:
                    text, _usage, model, provider = got
                    assert isinstance(text, str) and model == "m" and provider == "fallback"
        except BaseException as exc:  # noqa: BLE001 - reported from the main thread
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(llm._RESPONSE_CACHE) <= llm._RESPONSE_CACHE_MAX
    llm.clear_response_cache()
