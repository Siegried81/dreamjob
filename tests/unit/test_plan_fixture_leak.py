"""A test fixture may not reach a campaign plan (FR-161, FR-163, FR-164; C5).

This is the second time unit-test data has been found in the installed
database.  The first was the ``source_catalogue`` rows ``broken_board`` and
``stub_board``: ``sync_catalogue`` writes every adapter in the process-wide
registry (NFR-601), a full unit run has each module's stubs in that registry,
and one ``load_all()`` against the real ``DREAMJOB_DB_PATH`` catalogued them as
real sources.  Those rows are pruned at start-up now and
``tests/unit/conftest.py`` points the whole suite at a scratch file, so the
catalogue cannot be polluted again.

What nobody cleaned up was what the polluted catalogue *produced*: 104
``source_plan_item`` rows naming ``broken_board``, ``stub_board`` and eight
``spine.*`` stubs, spread over 32 campaigns - items a campaign schedules,
cannot run and cannot explain, and 104 of the 538 outcomes the FR-185
dashboard had to account for.  Migration 132 deletes
them; the guard in ``campaigns.insert_plan_item`` is what stops the next ones
being written: a plan item must name a source something knows about - an
implementation, or a catalogue row a person can act on.

The guard cannot use the ten real fixture names as its example, because in a
full unit run those stubs *are* registered adapters in this process - which is
exactly the leak's mechanism, and the reason the guard is the second net and
``conftest`` is the first.
"""

from __future__ import annotations

import sqlite3

import pytest
from dreamjob.adapters import discover
from dreamjob.adapters.base import all_adapters
from dreamjob.browser import glassdoor
from dreamjob.browser import linkedin as li
from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db import migrator
from dreamjob.db.connection import insert_row, query_all, query_one, upsert_row, utcnow, write_tx
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo

#: The stub adapter keys that reached the installed database, and that
#: migration 132 removes.  Ten of the suite's twenty stub keys; the other ten
#: were never planned before ``conftest`` closed the leak.
FIXTURE_KEYS = (
    "broken_board",
    "stub_board",
    "spine.ats",
    "spine.board",
    "spine.discovery",
    "spine.empty",
    "spine.greenhouse",
    "spine.hostile",
    "spine.refused",
    "spine.silent",
)

#: A key with a fixture's shape and no implementation anywhere, used where a
#: real fixture name would be a *registered* adapter in a full suite run.
NEVER_SHIPPED = "leaked_stub_board"

#: The reason collection writes on a plan item whose adapter has gone.
STALE_REASON = "no adapter registered for this source"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A scratch database with every migration applied, 132 included."""
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    # The shipped adapters register on import; the guard is inert until they
    # have, so a module run alone must import them exactly as the app does.
    discover()
    yield
    get_settings.cache_clear()


def _campaign() -> str:
    seeker = insert_row(
        "job_seeker",
        {
            "email": "leak@example.com",
            "display_name": "Leak",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker, "name": "Leak", "created_at": utcnow()}
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker,
        {"name": "Leak", "directive_set_id": directive_id, "profile_version_id": profile_id},
    )


def _item(campaign_id: str, adapter_key: str, **overrides) -> str:
    """A stored plan item for a source that cannot run, as the leak left them."""
    return campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": adapter_key, "native_query": {"queries": ["data"]}, **overrides},
        allow_unimplemented=True,
    )


# ---------------------------------------------------------------------------
# The door the fixtures came through
# ---------------------------------------------------------------------------


def test_a_unit_test_cannot_open_the_installed_database():
    """``tests/unit/conftest.py`` enforces the rule, rather than trusting it.

    The catalogue leak was a suite run with the real ``DREAMJOB_DB_PATH`` still
    in place; pointing the setting elsewhere fixes that, and this makes it
    impossible to undo by accident - a unit test that reaches for ``data/``
    gets an error naming C5 rather than a working connection.
    """
    installed = (REPO_ROOT / "data" / "dreamjob.db").resolve()

    with pytest.raises(RuntimeError, match="installed database"):
        sqlite3.connect(str(installed))

    assert get_settings().abs_db_path.resolve() != installed


def test_no_unit_test_can_reach_a_model_with_a_real_key():
    """The same rule for the other door: no LLM credential inside the suite.

    ``conftest._the_model_is_out_of_reach`` blanked ``DEEPSEEK_API_KEY`` only,
    while ``LLMClient.complete`` rotates over ``Settings.deepseek_api_keys`` -
    which still held the four ``DEEPSEEK_API_KEY_2..5`` values from ``.env`` -
    and then falls through to ``DREAMJOB_FALLBACK_LLM_API_KEY``, the paid
    endpoint.  The pass gates read the single field, so they degraded as
    intended, but any test that called ``complete()`` without stubbing
    ``httpx`` spent real money on real keys.  Asserted on counts and booleans,
    never on a value: a failing assertion must not print a credential.
    """
    settings = get_settings()

    assert settings.deepseek_api_key == ""
    assert len(settings.deepseek_api_keys) == 0, (
        "the rotation list is what complete() iterates; one blanked field is not enough"
    )
    assert settings.fallback_llm_api_key == "", "the paid fallback must be unreachable too"
    assert settings.local_llm_base_url == ""


# ---------------------------------------------------------------------------
# The guard: nothing is planned that nothing knows about
# ---------------------------------------------------------------------------


def test_a_source_nothing_knows_about_is_refused(db):
    """C5: the row a campaign schedules that nothing can run.

    No adapter, no catalogue row, no way for anyone to find out what it was
    meant to be - which is what the 104 fixture items had become by the time
    they were found.
    """
    campaign_id = _campaign()

    with pytest.raises(campaign_repo.UnknownSource) as raised:
        campaign_repo.insert_plan_item(campaign_id, {"adapter_key": NEVER_SHIPPED})

    assert NEVER_SHIPPED in str(raised.value)
    assert query_all("SELECT id FROM source_plan_item") == [], "and no row was written"


def test_a_plan_item_with_no_adapter_key_at_all_is_refused(db):
    """An item that names nothing can be executed by nothing either."""
    campaign_id = _campaign()

    with pytest.raises(campaign_repo.UnknownSource):
        campaign_repo.insert_plan_item(campaign_id, {"native_query": {}})


def test_a_catalogued_source_whose_adapter_is_gone_is_still_planned(db):
    """The tolerance the guard must not break (FR-164, FR-363).

    A catalogue row with no adapter is a source somebody can see, acknowledge
    or switch off, and the product has a settled answer for it: the item is
    planned, collection settles it as ``skipped``, and
    ``admin.prune_unknown_sources`` removes the row at the next start-up unless
    an administrator decided to keep it.  Refusing it here would break a whole
    plan over a row that is already being handled honestly - which is the
    opposite of what this guard is for.
    """
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": "not_implemented_yet",
            "display_name": "Not implemented yet",
            "source_type": "job_board",
            "access_method": "http",
            "enabled": 1,
            "requires_ack": 0,
            "tos_status": "permitted",
            "updated_at": utcnow(),
        },
        ["adapter_key"],
    )

    item_id = campaign_repo.insert_plan_item(
        _campaign(), {"adapter_key": "not_implemented_yet", "native_query": {}}
    )

    assert campaign_repo.get_plan_item(item_id) is not None


def test_a_shipped_source_is_planned_normally(db):
    """The guard must be invisible to every real source (FR-163)."""
    campaign_id = _campaign()

    item_id = campaign_repo.insert_plan_item(
        campaign_id, {"adapter_key": "board.eures", "native_query": {"queries": ["data"]}}
    )

    assert campaign_repo.get_plan_item(item_id)["adapter_key"] == "board.eures"


def test_the_browser_strategies_are_not_mistaken_for_fixtures(db):
    """FR-165: the LinkedIn network item is run by the browser, not an adapter.

    It is planned like any other source and is deliberately absent from the
    adapter registry, so a guard that only knew the registry would refuse to
    plan a feature the product ships.
    """
    assert li.ADAPTER_KEY not in all_adapters(), "the premise: it is not an adapter"
    assert campaign_repo.BROWSER_STRATEGY_KEYS == {li.ADAPTER_KEY, glassdoor.ADAPTER_KEY}, (
        "campaigns.py spells these keys out to avoid importing the browser stack; "
        "this is the assertion that stops the copy drifting"
    )

    campaign_id = _campaign()
    item_id = campaign_repo.insert_plan_item(
        campaign_id, {"adapter_key": li.ADAPTER_KEY, "native_query": {}}
    )

    assert campaign_repo.get_plan_item(item_id, campaign_id) is not None


def test_nothing_is_refused_before_the_adapters_are_imported(db, monkeypatch):
    """An empty registry means "not imported yet", never "nothing exists".

    ``prune_unknown_sources`` refuses to judge the catalogue for the same
    reason: a process that has not imported the adapter package cannot tell a
    missing adapter from an unimported one, and a guard that got this wrong
    would break every plan written by a caller that imports less than the app.
    """
    from dreamjob.adapters import base

    monkeypatch.setattr(base, "all_adapters", dict)
    assert campaign_repo.implemented_sources() == set()

    item_id = campaign_repo.insert_plan_item(_campaign(), {"adapter_key": NEVER_SHIPPED})

    assert campaign_repo.get_plan_item(item_id) is not None


def test_a_plan_item_may_deliberately_outlive_its_adapter(db):
    """The escape hatch, and why it is not a hole.

    A plan is stored and an adapter can be deleted or renamed after it was
    written, so collection has to settle an item whose adapter has gone
    (``status='skipped'``).  Constructing that state is the one legitimate
    reason to write an unimplemented item, it takes an explicit keyword, and
    nothing in the application passes it.
    """
    item_id = _item(_campaign(), NEVER_SHIPPED, status="skipped", last_error=STALE_REASON)

    assert campaign_repo.get_plan_item(item_id)["status"] == "skipped"


# ---------------------------------------------------------------------------
# Migration 132: what the leak already wrote
# ---------------------------------------------------------------------------


def _apply_132() -> None:
    sql = (migrator.MIGRATIONS_DIR / "132_fixture_cleanup.sql").read_text(encoding="utf-8")
    with write_tx() as conn:
        conn.executescript(sql)


def test_migration_132_is_part_of_the_schema_history(db):
    """It is a numbered migration, not a script somebody has to remember."""
    assert query_one("SELECT name FROM schema_migration WHERE version = '132'") is not None


def test_migration_132_deletes_every_fixture_plan_item(db):
    """The 104 rows, by the ten keys the installed database actually held."""
    campaign_id = _campaign()
    for key in FIXTURE_KEYS:
        _item(campaign_id, key)
    kept = campaign_repo.insert_plan_item(
        campaign_id, {"adapter_key": "board.eures", "native_query": {"queries": ["data"]}}
    )

    _apply_132()

    remaining = [r["adapter_key"] for r in query_all("SELECT adapter_key FROM source_plan_item")]
    assert remaining == ["board.eures"], "every fixture item is gone and the real one is not"
    assert campaign_repo.get_plan_item(kept)["status"] == "planned"


def test_migration_132_removes_a_leaked_catalogue_row_but_not_an_acknowledged_one(db):
    """A row an administrator acknowledged is a decision; the rest is residue."""
    for key, acknowledged in (("stub_board", None), ("spine.hostile", utcnow())):
        upsert_row(
            "source_catalogue",
            {
                "adapter_key": key,
                "display_name": key,
                "source_type": "job_board",
                "access_method": "http",
                "enabled": 1,
                "requires_ack": 0,
                "tos_status": "permitted",
                "acknowledged_at": acknowledged,
                "updated_at": utcnow(),
            },
            ["adapter_key"],
        )

    _apply_132()

    left = {r["adapter_key"] for r in query_all("SELECT adapter_key FROM source_catalogue")}
    assert "stub_board" not in left
    assert "spine.hostile" in left, "an acknowledged row is left for a human"


def test_migration_132_retires_the_items_whose_reason_became_untrue(db):
    """The four rows naming adapters that ship today (board.arbeitnow,
    board.actiris, ats.workable, ats.teamtailor), all in one cancelled
    campaign.

    Retired rather than re-planned: re-planning is the planner's job and the
    user's decision (FR-163), and reviving an item inside a finished campaign
    would put a source back into a plan nobody approved.  What had to change is
    the reason, which claimed a shipped adapter does not exist.
    """
    campaign_id = _campaign()
    stale = [
        _item(campaign_id, key, status="skipped", last_error=STALE_REASON)
        for key in ("board.arbeitnow", "board.actiris", "ats.workable", "ats.teamtailor")
    ]

    _apply_132()

    for item_id in stale:
        row = campaign_repo.get_plan_item(item_id)
        assert row["status"] == "skipped", "a cancelled campaign's item is not resurrected"
        assert row["last_error"] != STALE_REASON
        assert "re-plan" in row["last_error"], row["last_error"]
        assert row["records_collected"] == 0
