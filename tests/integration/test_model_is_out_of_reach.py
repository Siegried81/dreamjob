"""The guard in this directory's conftest is enforced, not trusted.

A load test that reaches a real model spends real money every time it runs, and
the only reason anyone noticed is the warning the client now logs when the paid
fallback answers. This pins the guard so the warning never has to fire again.
"""

from __future__ import annotations

from dreamjob.config import get_settings


def test_no_integration_test_can_reach_a_model_with_a_real_key() -> None:
    """Asserted on counts and booleans, never on a value.

    A failing assertion here must not print a credential into the pytest output,
    which is the worst way to learn that a key was in scope.
    """
    settings = get_settings()

    assert settings.deepseek_api_key == ""
    assert len(settings.deepseek_api_keys) == 0, (
        "the rotation list is what complete() iterates over; one blanked field is not enough"
    )
    assert settings.fallback_llm_api_key == "", "the paid fallback must be unreachable too"
    assert settings.local_llm_base_url == ""
