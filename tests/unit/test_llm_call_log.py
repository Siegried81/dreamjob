"""A call that produced nothing is logged as having produced nothing (FR-364).

The AI call log is the only record of what the models were asked and what came
back, so its worst failure mode is not a missing row - it is a row that says a
call succeeded when the feature that made it got nothing.  That is exactly what
happened: a reasoning model can spend its whole ``max_tokens`` budget on its
private chain of thought and return a 200 with an empty ``content``, and the
call was written to the log as ``status='ok'`` with an empty response.

These tests hold the client to the honest version: the tokens were spent, so
the row is written, and it is written as ``truncated`` or ``empty`` with the
reason on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dreamjob.db import connection as conn_mod
from dreamjob.db.migrator import migrate


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "calls.db"
    migrate(path)
    real = conn_mod.get_connection
    monkeypatch.setattr(conn_mod, "get_connection", lambda db_path=None: real(path))

    from dreamjob.llm.client import invalidate_admin_config

    invalidate_admin_config()
    yield path
    invalidate_admin_config()


class _Response:
    """Just enough of ``httpx.Response`` for ``LLMClient.complete``."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _answering(payload: dict[str, Any]):
    """A stand-in for ``httpx.Client`` that always answers with ``payload``."""

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, *args: Any, **kwargs: Any) -> _Response:
            return _Response(payload)

    return _Client


def _client():
    from dreamjob.llm.client import LLMClient

    client = LLMClient(job_seeker_id=None)
    # The routing refuses to call a provider with no key, and this test never
    # reaches a network.  The token rates are pinned too: they come from .env,
    # where a free-tier endpoint legitimately sets them to 0, and a test that
    # asserts on a non-zero cost would then fail for a configuration reason.
    client.settings = client.settings.model_copy(update={
        "deepseek_api_key": "test-key",
        "llm_cost_per_1m_input_eur": 0.25,
        "llm_cost_per_1m_output_eur": 1.00,
    })
    return client


def _last_call() -> dict[str, Any]:
    from dreamjob.db.connection import query_one

    row = query_one("SELECT * FROM llm_call ORDER BY created_at DESC, id DESC LIMIT 1")
    assert row is not None, "the call was never written to the FR-364 log at all"
    return dict(row)


def test_a_reasoner_that_answers_nothing_is_logged_as_truncated(db, monkeypatch):
    """The failure the e2e run found: 200, empty content, whole budget on reasoning."""
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import TruncatedResponse

    payload = {
        "choices": [
            {
                "message": {"content": "", "reasoning_content": "x" * 18_202},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 900, "completion_tokens": 4096},
    }
    monkeypatch.setattr(client_mod.httpx, "Client", _answering(payload))

    with pytest.raises(TruncatedResponse) as raised:
        _client().complete("score.opportunity", "system", "user", max_tokens=4096, retries=0)

    # The remedy has to be in the message, or the caller cannot act on it.
    assert "max_tokens" in str(raised.value)

    row = _last_call()
    assert row["status"] == "truncated", (
        f"an answerless call was logged as {row['status']!r}, which says it succeeded"
    )
    assert row["error"], "the row carries no reason for the empty response"
    assert not row["response_text"]
    # The tokens were spent whether or not an answer came back.
    assert row["output_tokens"] == 4096
    assert row["input_tokens"] == 900
    assert row["cost_eur"] > 0


def test_an_empty_answer_that_was_not_truncated_is_logged_as_empty(db, monkeypatch):
    """Finished cleanly and still said nothing: a different fault, named as one."""
    import dreamjob.llm.client as client_mod
    from dreamjob.llm.client import LLMError, TruncatedResponse

    payload = {
        "choices": [{"message": {"content": "   "}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 0},
    }
    monkeypatch.setattr(client_mod.httpx, "Client", _answering(payload))

    with pytest.raises(LLMError) as raised:
        _client().complete("classify.reply", "system", "user", retries=0)
    assert not isinstance(raised.value, TruncatedResponse)

    row = _last_call()
    assert row["status"] == "empty"
    assert row["error"]


def test_a_call_that_answered_is_still_logged_as_ok(db, monkeypatch):
    """The honest label in the other direction, so 'truncated' means something."""
    import dreamjob.llm.client as client_mod

    payload = {
        "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 12},
    }
    monkeypatch.setattr(client_mod.httpx, "Client", _answering(payload))

    result = _client().complete("classify.reply", "system", "user", retries=0)

    assert result.text == '{"ok": true}'
    row = _last_call()
    assert row["status"] == "ok"
    assert row["error"] is None
    assert row["response_text"] == '{"ok": true}'
