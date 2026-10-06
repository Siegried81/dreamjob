"""The near-real-time vacancy refresh and its scheduler task (FR-401, FR-261).

``pipeline/vacancy_refresh.py`` re-reads, every few minutes, a bounded slice of
the ATS boards behind existing opportunities and puts new postings straight
into each follower's ranked list.  The properties pinned down here:

* the slice is round-robin: it resumes after the stored cursor, wraps round,
  and survives the cursor company leaving the set;
* only boards behind a live campaign, with a known vendor and slug, are read;
* a new posting reaches every follower campaign and is announced once per
  seeker - a second pass over the same board announces nothing;
* one failing board is counted and does not stop the next;
* the scheduler task exists, runs every 10 minutes by default, and is switched
  off by ``DREAMJOB_VACANCY_REFRESH_SECONDS=0``.

No network: the ATS adapter is replaced by a stub that returns normalised
postings, and the real knowledge-base writer stores them - so "new" is decided
by the writer's own created/merged outcome, exactly as in production.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import subprocess
import sys
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import Settings, get_settings

_ENV_KEYS = ("DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY", "DREAMJOB_ENV")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_ENV"] = "development"
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixture data
# ---------------------------------------------------------------------------


def seed_campaign(email: str, *, status: str = "running") -> dict:
    """One seeker with one campaign, the minimum ``add_to_ranked_list`` reads."""
    from dreamjob.db.connection import insert_row, to_json, utcnow

    seeker_id = insert_row(
        "job_seeker",
        {"email": email, "display_name": email.split("@")[0], "locale": "en",
         "created_at": utcnow(), "updated_at": utcnow()},
    )
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Default", "created_at": utcnow()},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1,
         "sections": to_json({"summary": "Data platform lead"}), "created_at": utcnow()},
    )
    campaign_id = insert_row(
        "campaign",
        {"job_seeker_id": seeker_id, "directive_set_id": directive_id,
         "profile_version_id": profile_id, "name": "Autumn", "status": status,
         "created_at": utcnow()},
    )
    return {"seeker_id": seeker_id, "campaign_id": campaign_id}


def seed_company(name: str, *, vendor: str | None = "greenhouse", slug: str | None = None) -> str:
    from dreamjob.db.connection import insert_row, utcnow

    return insert_row(
        "company",
        {"normalised_name": name.lower(), "name": name, "country": "BE",
         "ats_vendor": vendor, "ats_slug": slug if slug is not None else name.lower(),
         "collected_at": utcnow()},
    )


def insert_vacancy(company_id: str, title: str, collected_at: str | None = None) -> str:
    from dreamjob.db.connection import insert_row, to_json, utcnow

    return insert_row(
        "vacancy",
        {"company_id": company_id, "title": title, "description": f"{title} in Ghent.",
         "required_skills": to_json(["Python"]), "location": "Ghent", "country": "BE",
         "collected_at": collected_at or utcnow()},
    )


def follow(campaign: dict, company_id: str) -> str:
    """Give ``campaign`` an opportunity at ``company_id``, which makes it a follower."""
    from dreamjob.db.connection import insert_row, utcnow

    vacancy_id = insert_vacancy(company_id, "Existing role")
    return insert_row(
        "opportunity",
        {"job_seeker_id": campaign["seeker_id"], "campaign_id": campaign["campaign_id"],
         "company_id": company_id, "vacancy_id": vacancy_id, "kind": "vacancy",
         "title": "Existing role", "country": "BE", "language": "en", "score": 70.0,
         "created_at": utcnow(), "updated_at": utcnow()},
    )


class FakeBoards:
    """Stands in for ``get_adapter``: each board is an adapter that never fetches.

    ``postings[company_id]`` lists the titles a board shows; every read returns
    all of them as normalised vacancy records, so the first read creates rows
    and every later read is a merge - the knowledge-base writer decides which.
    A company id in ``failing`` raises the way an unreachable board does.
    """

    def __init__(self, postings: dict[str, list[str]], failing: set[str] | None = None):
        self.postings = postings
        self.failing = failing or set()
        self.read: list[str] = []

    def __call__(self, adapter_key, egress):
        boards = self

        class _Adapter:
            async def run(self, item):
                from dreamjob.adapters.base import NormalisedRecord

                company_id = item.native_query["company_id"]
                boards.read.append(company_id)
                if company_id in boards.failing:
                    raise RuntimeError("board answered 500")
                return [
                    NormalisedRecord(
                        "vacancy",
                        {"company_id": company_id, "title": title,
                         "description": f"{title} in Ghent.", "location": "Ghent",
                         "country": "BE",
                         "source_url": f"https://boards.example/{item.native_query['slug']}/"
                                       f"{title.replace(' ', '-').lower()}"},
                    )
                    for title in boards.postings.get(company_id, [])
                ]

        return _Adapter()


def vacancy_id_for(company_id: str, title: str) -> str:
    from dreamjob.db.connection import query_one

    return query_one(
        "SELECT id FROM vacancy WHERE company_id = ? AND title = ?", (company_id, title)
    )["id"]


def refresh(monkeypatch, boards: FakeBoards, **kwargs) -> dict:
    from dreamjob.pipeline import vacancy_refresh

    monkeypatch.setattr(vacancy_refresh, "get_adapter", boards)
    return asyncio.run(vacancy_refresh.refresh(**kwargs))


# ---------------------------------------------------------------------------
# next_slice
# ---------------------------------------------------------------------------


def _boards(*keys: str) -> OrderedDict:
    return OrderedDict((k, {"company_id": k}) for k in keys)


def _ids(rows: list[dict]) -> list[str]:
    return [r["company_id"] for r in rows]


def test_without_a_cursor_the_slice_starts_at_the_first_board() -> None:
    from dreamjob.pipeline.vacancy_refresh import next_slice

    assert _ids(next_slice(_boards("a", "b", "c"), None, 2)) == ["a", "b"]


def test_the_slice_resumes_after_the_cursor_and_wraps_round() -> None:
    from dreamjob.pipeline.vacancy_refresh import next_slice

    boards = _boards("a", "b", "c", "d")
    assert _ids(next_slice(boards, "a", 2)) == ["b", "c"]
    assert _ids(next_slice(boards, "c", 3)) == ["d", "a", "b"]
    assert _ids(next_slice(boards, "d", 2)) == ["a", "b"]
    assert _ids(next_slice(boards, "b", 10)) == ["c", "d", "a", "b"], "never more than the set"


def test_a_cursor_that_left_the_set_resumes_at_the_next_id() -> None:
    from dreamjob.pipeline.vacancy_refresh import next_slice

    boards = _boards("a", "c", "e")
    assert _ids(next_slice(boards, "b", 2)) == ["c", "e"]
    assert _ids(next_slice(boards, "z", 2)) == ["a", "c"], "past the last id wraps to the start"


def test_an_empty_set_or_size_gives_nothing() -> None:
    from dreamjob.pipeline.vacancy_refresh import next_slice

    assert next_slice(OrderedDict(), "a", 5) == []
    assert next_slice(_boards("a"), None, 0) == []


# ---------------------------------------------------------------------------
# ranked_ats_boards
# ---------------------------------------------------------------------------


def test_only_boards_behind_a_live_campaign_with_vendor_and_slug_are_followed() -> None:
    from dreamjob.db.repositories import pipeline_cards as repo

    live = seed_campaign("live@example.test")
    cancelled = seed_campaign("gone@example.test", status="cancelled")
    followed = seed_company("Northwind")
    only_cancelled = seed_company("Contoso")
    no_vendor = seed_company("Fabrikam", vendor=None)
    no_slug = seed_company("Tailspin", slug="")
    follow(live, followed)
    follow(cancelled, only_cancelled)
    follow(live, no_vendor)
    follow(live, no_slug)

    rows = repo.ranked_ats_boards()

    assert [(r["company_id"], r["campaign_id"]) for r in rows] == [
        (followed, live["campaign_id"])
    ]
    assert rows[0]["ats_vendor"] == "greenhouse" and rows[0]["ats_slug"] == "northwind"


# ---------------------------------------------------------------------------
# refresh()
# ---------------------------------------------------------------------------


def test_nothing_followed_is_a_skip() -> None:
    from dreamjob.pipeline import vacancy_refresh

    assert asyncio.run(vacancy_refresh.refresh()) == {"skipped": "no followed ATS boards"}


def test_a_new_posting_reaches_every_follower_and_is_announced_once(monkeypatch) -> None:
    from dreamjob.db.repositories import opportunities as opp_repo
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.monitoring import watchlist

    first = seed_campaign("ann@example.test")
    second = seed_campaign("bob@example.test")
    company = seed_company("Northwind")
    follow(first, company)
    follow(second, company)

    calls: list[tuple[str, str, list[str]]] = []
    real_add = watchlist.add_to_ranked_list

    def recording_add(seeker_id, campaign_id, vacancies):
        calls.append((seeker_id, campaign_id, [v["title"] for v in vacancies]))
        return real_add(seeker_id, campaign_id, vacancies)

    monkeypatch.setattr(watchlist, "add_to_ranked_list", recording_add)
    boards = FakeBoards({company: ["Analytics Engineer"]})

    report = refresh(monkeypatch, boards)

    assert sorted((s, c) for s, c, _ in calls) == sorted(
        [(first["seeker_id"], first["campaign_id"]), (second["seeker_id"], second["campaign_id"])]
    )
    assert all(titles == ["Analytics Engineer"] for *_, titles in calls)
    assert report["boards"] == 1 and report["errors"] == 0
    assert report["new_vacancies"] == 1
    assert report["opportunities_added"] == 2 and report["notifications"] == 2

    vacancy_id = vacancy_id_for(company, "Analytics Engineer")
    for campaign in (first, second):
        assert opp_repo.find_by_vacancy(campaign["campaign_id"], vacancy_id) is not None
        notes = repo.list_notifications(campaign["seeker_id"], kind="new_vacancy")
        assert len(notes) == 1
        assert notes[0]["dedup_key"] == f"vacancy:{vacancy_id}"

    # The next pass re-reads the same board: the posting is merged, not new -
    # even though the merge re-stamps its ``collected_at``.
    again = refresh(monkeypatch, boards)
    assert again["new_vacancies"] == 0
    assert again["opportunities_added"] == 0 and again["notifications"] == 0
    for campaign in (first, second):
        assert len(repo.list_notifications(campaign["seeker_id"], kind="new_vacancy")) == 1


def test_a_failing_board_does_not_stop_the_next(monkeypatch) -> None:
    from dreamjob.db.repositories import pipeline_cards as repo

    campaign = seed_campaign("ann@example.test")
    broken = seed_company("Contoso")
    healthy = seed_company("Northwind")
    follow(campaign, broken)
    follow(campaign, healthy)
    boards = FakeBoards({healthy: ["Analytics Engineer"]}, failing={broken})

    report = refresh(monkeypatch, boards)

    assert set(boards.read) == {broken, healthy}
    assert report["boards"] == 2 and report["errors"] == 1
    assert report["notifications"] == 1
    assert len(repo.list_notifications(campaign["seeker_id"], kind="new_vacancy")) == 1


def test_a_board_whose_vendor_has_no_adapter_is_counted(monkeypatch) -> None:
    from dreamjob.pipeline import vacancy_refresh

    campaign = seed_campaign("ann@example.test")
    follow(campaign, seed_company("Northwind", vendor="nosuchats"))

    report = asyncio.run(vacancy_refresh.refresh())

    assert report["boards"] == 1 and report["errors"] == 1 and report["new_vacancies"] == 0


def test_the_cursor_advances_round_robin_across_passes(monkeypatch) -> None:
    from dreamjob.db.repositories import admin as admin_repo
    from dreamjob.db.repositories import pipeline_cards as repo
    from dreamjob.pipeline.vacancy_refresh import SETTING_CURSOR

    campaign = seed_campaign("ann@example.test")
    companies = [seed_company(name) for name in ("Contoso", "Northwind", "Fabrikam")]
    for company_id in companies:
        follow(campaign, company_id)
    ordered = [r["company_id"] for r in repo.ranked_ats_boards()]
    boards = FakeBoards({})

    for expected in [ordered[0], ordered[1], ordered[2], ordered[0]]:
        refresh(monkeypatch, boards, limit=1)
        assert boards.read[-1] == expected
        assert admin_repo.get_setting(SETTING_CURSOR) == expected


# ---------------------------------------------------------------------------
# Settings and the scheduler task
# ---------------------------------------------------------------------------


def test_the_settings_defaults() -> None:
    assert Settings.model_fields["vacancy_refresh_seconds"].default == 600
    assert Settings.model_fields["vacancy_cache_ttl_seconds"].default == 3600


def test_the_task_is_registered_with_the_configured_interval() -> None:
    from dreamjob.monitoring import scheduler as scheduler_mod

    task = next(t for t in scheduler_mod.DEFAULT_TASKS if t.name == "vacancy_refresh")
    configured = get_settings().vacancy_refresh_seconds
    assert task.interval_seconds == max(60, configured)
    assert task.enabled is (configured > 0)


def _task_in_fresh_process(tmp_path: Path, seconds: str) -> str:
    """``(interval, enabled)`` of the task as a new process with this setting builds it.

    The task list is built at import time, so a fresh interpreter is the honest
    way to read it under another setting without reloading the module the rest
    of the suite holds.
    """
    env = {**os.environ, "DREAMJOB_VACANCY_REFRESH_SECONDS": seconds,
           "DREAMJOB_DB_PATH": str(tmp_path / "data" / "sub.db"),
           "DREAMJOB_DATA_DIR": str(tmp_path / "data"),
           "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "backend")}
    code = (
        "from dreamjob.monitoring import scheduler as s;"
        "t = next(t for t in s.DEFAULT_TASKS if t.name == 'vacancy_refresh');"
        "print(t.interval_seconds, t.enabled)"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=120, check=True)
    return out.stdout.strip().splitlines()[-1]


def test_the_task_runs_every_ten_minutes_by_default(tmp_path: Path) -> None:
    assert _task_in_fresh_process(tmp_path, "600") == "600 True"


def test_zero_switches_the_task_off(tmp_path: Path) -> None:
    assert _task_in_fresh_process(tmp_path, "0").endswith("False")
