"""No integration test may spend tokens on a real model.

``tests/unit/conftest.py`` already enforces this, and its docstring explains
why at length - but a conftest only governs the directory it sits in, so the
guard stopped at ``tests/unit/`` and this directory ran with whatever is in
``.env``. It is not hypothetical: a single run of
``test_a_campaign_resumes_across_a_restart_without_losing_or_repeating_work``
was observed to take ``score.opportunity`` to ``api.groq.com`` with a live key,
get a 429, and then fall through to the **paid** ``api.deepseek.com`` endpoint,
which answered. One billed call per run of a load test, on every machine with a
filled-in ``.env``.

The module's own ``offline`` fixture patches ``EgressClient.fetch``, which is
the door the *adapters* use; the model client is a different door and goes
through ``httpx`` directly.

Every credential that reaches a model is blanked, not only the first:
``LLMClient.complete`` rotates over ``Settings.deepseek_api_keys``, which is
built from ``DEEPSEEK_API_KEY`` *and* ``DEEPSEEK_API_KEY_2..5``, and then falls
back to ``DREAMJOB_FALLBACK_LLM_API_KEY``. The scoring pass degrades to its
deterministic path when no model is configured, which is what an integration
test of the pipeline wants anyway.

``DREAMJOB_TEST_ALLOW_MODEL=1`` lifts the guard, the same escape hatch the unit
conftest documents, so a deliberate live run is still possible.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from dreamjob.config import get_settings

#: Every setting that can put a request in front of a real model.
MODEL_CREDENTIAL_ENV = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY_2",
    "DEEPSEEK_API_KEY_3",
    "DEEPSEEK_API_KEY_4",
    "DEEPSEEK_API_KEY_5",
    "DREAMJOB_FALLBACK_LLM_API_KEY",
    "DREAMJOB_LOCAL_LLM_BASE_URL",
)


@pytest.fixture(scope="session", autouse=True)
def _the_model_is_out_of_reach() -> Iterator[None]:
    """Blank every model credential for the whole session. See the module docstring."""
    if os.environ.get("DREAMJOB_TEST_ALLOW_MODEL") == "1":
        yield
        return
    patch = pytest.MonkeyPatch()
    for name in MODEL_CREDENTIAL_ENV:
        patch.setenv(name, "")
    get_settings.cache_clear()
    yield
    patch.undo()
    get_settings.cache_clear()
