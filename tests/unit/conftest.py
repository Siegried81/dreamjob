"""No unit test may touch the installed database (plan item C5).

Two rows named ``broken_board`` and ``stub_board`` sat in the installed
``source_catalogue`` for months and were selected as real sources by every
campaign, and fourteen more had joined them by the time anyone looked.  They
were not seeded by a migration or by an operator: they are stub adapters that
unit modules register in the process-wide adapter registry (NFR-601), and one
test class called ``load_all()`` without pointing the database anywhere, so
``sync_catalogue`` wrote every stub in the registry - including the ones other
modules had registered - into ``data/dreamjob.db``.

Individually each module was innocent; the leak only appeared when the whole
suite ran in one process, which is why it survived so long.  Rather than
asking every future test to remember the fixture, the whole unit suite is
pointed at a scratch database here, before any module fixture runs.  Modules
that want isolation still create their own ``tmp_path`` database on top of
this; ``monkeypatch`` then restores the scratch path rather than the installed
one when they are done.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db.migrator import migrate


@pytest.fixture(scope="session", autouse=True)
def scratch_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point ``DREAMJOB_DB_PATH`` at a throw-away file for the whole session."""
    root = tmp_path_factory.mktemp("dreamjob-unit")
    patch = pytest.MonkeyPatch()
    patch.setenv("DREAMJOB_DATA_DIR", str(root / "data"))
    patch.setenv("DREAMJOB_DB_PATH", str(root / "data" / "unit.db"))
    # No background scheduler in tests: it would run monitoring tasks against
    # the scratch database on its first tick and make the suite order-dependent.
    patch.setenv("DREAMJOB_SCHEDULER_ENABLED", "0")
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    yield
    patch.undo()
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _the_model_is_out_of_reach() -> Iterator[None]:
    """No unit test may spend tokens on a real model.

    The same class of leak as the installed database above, through a
    different door.  Collection now ends in the passes that rank what it
    collected, and the last of those reaches for the model when one is
    configured (FR-281) - so on a machine with a real ``DEEPSEEK_API_KEY`` in
    its environment, tests that merely run a campaign to completion started
    making live, billed API calls.  Ten seconds of one test run was three
    HTTPS round trips to a paid endpoint.

    A unit test that wants to exercise model behaviour injects a fake client,
    as ``_CountingLLM`` and its like already do, so the credentials are simply
    absent for the whole session and every pass that looks for a model
    degrades to its deterministic path.

    *Every* credential that reaches a model, not only the first.  Blanking
    ``DEEPSEEK_API_KEY`` alone left ``Settings.deepseek_api_keys`` holding the
    four rotation keys from ``.env`` - and ``LLMClient.complete`` rotates over
    that list, not over the single field - plus a live
    ``DREAMJOB_FALLBACK_LLM_API_KEY`` pointing at the paid endpoint.  The pass
    gates all read the single field, so they still degraded, but any test that
    called ``complete()`` without stubbing ``httpx`` spent real money on real
    keys, which is the exact leak this fixture is named after.

    The exception is deliberate and has to stay reachable: the prompt
    regression checks in ``test_employer_website_rung`` mean to call a real
    model and skip themselves when none is configured.  Setting
    ``DREAMJOB_TEST_ALLOW_MODEL=1`` lifts this guard for a run, which is how
    those are meant to be exercised - never as the default, because the
    default is what CI and every casual ``pytest`` invocation gets.
    """
    if os.environ.get("DREAMJOB_TEST_ALLOW_MODEL") == "1":
        yield
        return
    patch = pytest.MonkeyPatch()
    for name in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_2", "DEEPSEEK_API_KEY_3",
        "DEEPSEEK_API_KEY_4", "DEEPSEEK_API_KEY_5",
        "DREAMJOB_FALLBACK_LLM_API_KEY", "DREAMJOB_LOCAL_LLM_BASE_URL",
    ):
        patch.setenv(name, "")
    get_settings.cache_clear()
    yield
    patch.undo()
    get_settings.cache_clear()



@pytest.fixture(autouse=True)
def _fresh_connection_cache() -> Iterator[None]:
    """Never hand a test a connection another test opened.

    ``db.connection`` caches one SQLite connection per thread per path string,
    and ``pyproject`` sets ``tmp_path_retention_policy = "failed"``, so a
    passing test's ``tmp_path`` is deleted and pytest hands the *same numbered
    path* to a later test.  The path string matches, the cache hits, and the
    second test writes into the first one's deleted inode - which still holds
    the first one's rows.  The symptom is a mystery ``UNIQUE constraint
    failed: job_seeker.email`` in a module that isolates itself properly and
    passes when run alone (tests/unit/test_mail_dispatch.py).

    Forgetting the cache around every test costs microseconds - SQLite opens a
    file - and removes the whole class.  The connections are dropped rather
    than closed first, so a test that is still holding one keeps working.
    """
    from dreamjob.db import connection as _c

    def _forget() -> None:
        for key in [k for k in vars(_c._local) if k.startswith("conn_")]:
            conn = getattr(_c._local, key, None)
            delattr(_c._local, key)
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - a connection still in use is not ours to break
                pass

    # The LLM response cache is process-global; an answer one test cached must
    # not satisfy the next test's identical-looking call.
    try:
        from dreamjob.llm.client import clear_response_cache

        clear_response_cache()
    except Exception:  # noqa: BLE001 - the cache is an optimisation, never a dependency
        pass

    _forget()
    yield
    try:
        from dreamjob.llm.client import clear_response_cache

        clear_response_cache()
    except Exception:  # noqa: BLE001
        pass
    _forget()


@pytest.fixture(scope="session", autouse=True)
def _installed_database_is_out_of_reach(scratch_database: None) -> Iterator[None]:
    """Make the leak impossible, not merely unlikely (plan item C5).

    Pointing ``DREAMJOB_DB_PATH`` at a scratch file is enough right up until
    something resolves a path of its own, or reads the setting before the patch
    lands, and then the suite writes into ``data/dreamjob.db`` again and nobody
    notices for weeks.  That is not hypothetical: it is how ``stub_board`` and
    ``broken_board`` were catalogued as real sources, and the 104 plan items
    naming test stubs that migration 132 had to delete are what it cost.

    ``sqlite3.connect`` is the one door every database access goes through, so
    the rule is enforced at the door: no unit test may open a database inside
    the repository's ``data/`` directory, whatever it thinks its settings say.
    A test that genuinely needs the installed data is not a unit test.
    """
    installed = (REPO_ROOT / "data").resolve()
    real_connect = sqlite3.connect

    def _is_installed(database: object) -> bool:
        text = str(database)
        if not text or text == ":memory:":
            return False
        if str(installed) in text:  # absolute path, or a file: URI naming one
            return True
        try:
            return installed in Path(text).resolve().parents
        except (OSError, ValueError, RuntimeError):
            return False

    def guarded(database, *args, **kwargs):  # noqa: ANN001, ANN202 - sqlite3's own signature
        if _is_installed(database):
            raise RuntimeError(
                f"A unit test tried to open the installed database ({database!r}). "
                "Unit tests run against the scratch file this conftest creates; writing "
                "to data/ is how test fixtures were catalogued as real sources (C5). "
                "Use tmp_path, or read the installed data from a script instead."
            )
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = guarded
    try:
        yield
    finally:
        sqlite3.connect = real_connect
