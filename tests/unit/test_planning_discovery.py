"""Discovery and per-target planning (FR-162, FR-163, FR-186, DR-101).

The first gathering phase collected nothing at all, and the cause was not a
missing source: the planner emitted one plan item per *adapter*, handed every
ATS and registry item an empty target list, and the run then reported "done, 0
records, 0 errors" for each of them.  These tests hold the fix in place - a
discovery pass that materialises one target per board and per EURES partition
before anything is translated - and they assert the old silence is now audible:
a source with no target is rejected with a reason instead of being planned with
an empty query.

Everything runs against a throw-away SQLite file, with no network and no LLM.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from dreamjob.adapters.ats.common import ATSAdapter
from dreamjob.adapters.base import AdapterCapabilities
from dreamjob.adapters.jobboards.eures import EuresAdapter
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.pipeline import discovery, planning


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "t@example.com", "display_name": "T", "created_at": utcnow(),
         "updated_at": utcnow()},
    )


def _campaign(seeker_id: str, *, caps: dict | None = None, company_type: dict | None = None) -> str:
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Benelux data leadership",
            "job_content": {"roles": ["Data Engineer"]},
            "location": {"countries": ["Belgium", "NL"]},
            "company_type": company_type or {},
            "created_at": utcnow(),
        },
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {
            "name": "Autumn search",
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "caps": caps if caps is not None else {"max_pages": 10_000},
        },
    )


_ATS_CAPABILITIES = AdapterCapabilities(
    keyword_search=False, company_lookup=True, pagination=False, max_results_per_query=500
).__dict__


def _catalogue(adapter_key: str, **overrides) -> None:
    row = {
        "adapter_key": adapter_key,
        "display_name": adapter_key,
        "source_type": "job_board",
        "coverage_countries": ["BE", "NL"],
        "coverage_industries": [],
        "query_capabilities": AdapterCapabilities().__dict__,
        "access_method": "http",
        "rate_limit_rps": 0.5,
        "cost_per_call_eur": 0.0,
        "tos_status": "permitted",
        "enabled": 1,
        "requires_ack": 0,
        "updated_at": utcnow(),
    }
    row.update(overrides)
    upsert_row("source_catalogue", row, ["adapter_key"])


def _ats_catalogue(*vendors: str) -> None:
    for vendor in vendors:
        _catalogue(
            f"ats.{vendor}",
            source_type="ats",
            access_method="api",
            coverage_countries=[],
            query_capabilities=_ATS_CAPABILITIES,
            rate_limit_rps=0.5,
        )


def _registry(monkeypatch, tmp_path, boards: list[dict], name: str = "board_registry.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"boards": boards}), encoding="utf-8")
    monkeypatch.setattr(discovery, "DEFAULT_REGISTRY_PATH", path)
    return path


def _items(summary: dict, adapter_key: str) -> list[dict]:
    return [i for i in summary["items"] if i["adapter_key"] == adapter_key]


# ---------------------------------------------------------------------------
# N1: one plan item per target
# ---------------------------------------------------------------------------


def test_every_registry_board_becomes_its_own_plan_item(db, tmp_path, monkeypatch):
    """FR-162: a board is the unit of work, and every board gets an item.

    Before discovery there was one ``ats.greenhouse`` item for a whole campaign,
    it carried no slug, and ``slug_of()`` would have read only the first slug of
    a list anyway - so at most one board per vendor was ever readable.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _ats_catalogue("greenhouse", "lever")
    _registry(
        monkeypatch, tmp_path,
        [
            {"vendor": "greenhouse", "slug": "acme", "name": "Acme", "country": "BE"},
            {"vendor": "greenhouse", "slug": "bravo", "name": "Bravo", "country": "US"},
            {"vendor": "greenhouse", "slug": "charlie"},
            {"vendor": "lever", "slug": "delta", "name": "Delta NV", "country": "NL"},
            {"vendor": "workable", "slug": "echo"},          # no adapter yet: not planned
        ],
    )

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    greenhouse = _items(summary, "ats.greenhouse")
    lever = _items(summary, "ats.lever")
    assert len(greenhouse) == 3, "one item per board, not one item per vendor"
    assert len(lever) == 1
    assert {i["native_query"]["slug"] for i in greenhouse} == {"acme", "bravo", "charlie"}
    # Every item names exactly one board, so nothing is dropped by a reader that
    # takes the first slug of a list (adapters/ats/common.py slug_of).
    for item in greenhouse + lever:
        assert ATSAdapter.slugs_of(item) == [item["native_query"]["slug"]]
        assert item["estimated_pages"] == 1, "an ATS page is one whole board"
        assert item["estimated_cost_eur"] == 0.0, "a board is parsed without an LLM (C7)"
    assert not _items(summary, "ats.workable"), "a vendor with no adapter is not planned"
    assert summary["discovery"]["boards"]["registry"] == 4


def test_a_source_with_no_target_is_rejected_with_a_reason(db, tmp_path, monkeypatch):
    """The failure that used to be recorded as success (FR-164, FR-185).

    With no registry, no known board and no named company there is nothing for
    an ATS adapter to read.  That has to be visible on the review screen: the
    source is rejected with a stated reason, never planned with an empty query
    and then reported as a completed run that collected nothing.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _ats_catalogue("greenhouse")
    _registry(monkeypatch, tmp_path, [])

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert not _items(summary, "ats.greenhouse")
    rejected = {r["adapter_key"]: r["reason"] for r in summary["rejected_sources"]}
    assert "ats.greenhouse" in rejected
    assert "discovery" in rejected["ats.greenhouse"]
    assert summary["totals"]["sources"] == 0


def test_a_named_company_is_planned_from_the_url_the_seeker_gave(db, tmp_path, monkeypatch):
    """FR-143: the companies the job seeker names are targets in their own right."""
    seeker = _seeker()
    campaign_id = _campaign(
        seeker,
        company_type={
            "include_companies": [
                {"name": "Acme NV", "domain": "https://jobs.lever.co/acme-nv"},
                {"name": "Bravo BV", "ats_vendor": "greenhouse", "ats_slug": "bravo"},
                {"name": "Charlie", "domain": "https://charlie.example"},  # no board to find
            ]
        },
    )
    _ats_catalogue("greenhouse", "lever")
    _registry(monkeypatch, tmp_path, [])

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert [i["native_query"]["slug"] for i in _items(summary, "ats.lever")] == ["acme-nv"]
    assert [i["native_query"]["slug"] for i in _items(summary, "ats.greenhouse")] == ["bravo"]
    assert summary["discovery"]["boards"]["directives"] == 2


def test_a_board_the_knowledge_base_already_knows_is_planned(db, tmp_path, monkeypatch):
    """FR-342: what an earlier campaign discovered is read without re-discovering it."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _ats_catalogue("recruitee")
    _registry(monkeypatch, tmp_path, [])
    insert_row(
        "company",
        {
            "normalised_name": "delta", "name": "Delta NV", "country": "BE",
            "ats_vendor": "recruitee", "ats_slug": "delta", "collected_at": utcnow(),
        },
    )

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    items = _items(summary, "ats.recruitee")
    assert [i["native_query"]["slug"] for i in items] == ["delta"]
    assert items[0]["native_query"]["company_name"] == "Delta NV"


def test_boards_never_enter_the_company_inventory(db, tmp_path, monkeypatch):
    """Section 2.8: a board slug is a route, not a company to look up in a register.

    A registry board carries no legal identifier, so planning a statutory-filing
    lookup for each of them spends hours of rate-limited requests on nothing.
    Boards travel to the ATS adapters through ``caps["ats"]`` and stop there.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _ats_catalogue("greenhouse")
    _catalogue("registry.kbo", source_type="registry", coverage_countries=["BE"])
    _registry(
        monkeypatch, tmp_path,
        [{"vendor": "greenhouse", "slug": f"board-{n}"} for n in range(50)],
    )

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert len(_items(summary, "ats.greenhouse")) == 50
    assert not _items(summary, "registry.kbo"), "no company identity, so no register lookup"


def test_a_query_naming_three_boards_becomes_three_plan_items(db, tmp_path, monkeypatch):
    """FR-162: ``slug_of()`` reads the first slug, so a list of three lost two.

    The board after the first was dropped without a word and the plan item was
    still reported as a success.  Whatever route wrote the list - a model answer
    for a source that cannot plan itself, or a plan persisted by an older
    version - one item now names exactly one board.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    # A catalogue row with no adapter registered: the one route on which a
    # model-authored query still reaches the plan unshaped.
    _catalogue(
        "ats.unwritten", source_type="ats", access_method="api", coverage_countries=[],
        query_capabilities=_ATS_CAPABILITIES,
    )
    _registry(monkeypatch, tmp_path, [])

    class _ListLLM:
        campaign_id = None

        class budget:  # noqa: N801 - mirrors LLMClient.budget
            @staticmethod
            def should_degrade() -> bool:
                return False

        def complete_json(self, *a, **kw):
            return {
                "plans": [
                    {
                        "adapter_key": "ats.unwritten",
                        "native_query": {"board_slugs": ["acme", "bravo", "charlie"]},
                        "rationale": "three boards",
                        "estimated_pages": 1,
                    }
                ]
            }

    summary = planning.generate_plan(campaign_id, seeker, use_llm=True, llm=_ListLLM())

    items = _items(summary, "ats.unwritten")
    assert len(items) == 3
    assert {i["native_query"]["slug"] for i in items} == {"acme", "bravo", "charlie"}
    for item in items:
        assert "board_slugs" not in item["native_query"]
        assert ATSAdapter.slugs_of(item) == [item["native_query"]["slug"]]


# ---------------------------------------------------------------------------
# N2: EURES partitioning
# ---------------------------------------------------------------------------


def test_eures_is_partitioned_over_every_sector_a_campaign_can_act_on():
    """Section 5.2 N2: width buys employers, depth buys duplicates.

    The sector letters are NACE Rev 2.1, which is what the EURES facet uses:
    N is professional services and O is administrative and support, including
    the staffing agencies.  Both are swept.  Agencies are handled by the
    per-employer tag (``company_employer_kind``), never by declining to fetch
    a section - a vacancy is filed under every section on its employer's list,
    so an exclusion loses whole legitimate sectors and still removes no
    agencies (docs/Interim_Agencies_Proposal.md section 2.5).
    """
    items = discovery.eures_partitions(
        countries=["BE", "NL"], caps={"max_pages_per_source": 20}, partitioned=True
    )

    assert len(items) == 133, "7 NUTS-1 regions x 19 NACE Rev 2.1 sections"
    sections = {i.native_query["nace_section"] for i in items}
    assert "N" in sections, "N is professional services; excluding it removed no agency row"
    assert "O" in sections, "O is where staffing sits, and the tag - not the sweep - handles it"
    assert sections == set(discovery.NACE_SECTIONS)
    assert discovery.EXCLUDED_NACE_SECTIONS == {"T", "U", "V"}
    assert {i.native_query["nuts_codes"][0] for i in items} == {
        "BE1", "BE2", "BE3", "NL1", "NL2", "NL3", "NL4"
    }
    assert {i.native_query["publication_period"] for i in items} == {"LAST_MONTH"}
    # europa.eu asks for ten seconds between requests and the product honours
    # it (FR-182), so the sweep is sized in requests, not in pages.
    requests = sum(i.estimated_pages for i in items)
    assert requests <= discovery.EURES_REQUEST_BUDGET
    assert requests == 518, "restoring N costs 28 requests; halving O gives 14 of them back"
    assert all(1 <= i.estimated_pages <= 20 for i in items)
    assert all(i.native_query["fetch_details"] is False for i in items)


def test_the_broadest_section_gets_fewer_pages_rather_than_none():
    """O carries 86% of the region's rows, so its deep pages are duplicates.

    The lever for an agency-heavy partition is the page budget, not exclusion:
    every section still runs, and no section can be weighted down to nothing.
    """
    items = discovery.eures_partitions(
        countries=["BE"], caps={"max_pages_per_source": 20}, partitioned=True
    )
    pages = {i.native_query["nace_section"]: i.estimated_pages for i in items}

    assert pages["O"] < pages["C"], "the widest partition buys the fewest new employers per page"
    assert pages["O"] >= 1, "fewer pages, never no pages"
    assert pages["N"] == pages["C"], "professional services is an ordinary partition"

    # A campaign may re-weight; it may not zero a section out through the cap.
    zeroed = discovery.eures_partitions(
        countries=["BE"],
        caps={"max_pages_per_source": 20, "eures_section_page_weights": {"O": 0}},
        partitioned=True,
    )
    assert all(i.estimated_pages >= 1 for i in zeroed)


def test_an_unpartitioned_eures_adapter_is_not_asked_for_the_same_page_133_times():
    """The planner only partitions a sweep the adapter can actually execute."""
    items = discovery.eures_partitions(
        countries=["BE", "NL"], caps={"max_pages_per_source": 20}, partitioned=False
    )

    assert len(items) == 2
    queries = [json.dumps(i.native_query, sort_keys=True) for i in items]
    assert len(set(queries)) == len(queries), "no request is spent twice"


def test_generate_plan_partitions_eures_when_the_adapter_reads_the_keys(
    db, tmp_path, monkeypatch
):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("board.eures", source_type="job_board", access_method="api", rate_limit_rps=0.5)
    _registry(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        EuresAdapter, "PARTITION_QUERY_KEYS", discovery.EURES_PARTITION_KEYS, raising=False
    )

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert summary["discovery"]["eures"]["partitions"] == 133
    assert summary["discovery"]["eures"]["partitioned"] is True
    assert summary["totals"]["estimated_cost_eur"] == 0.0, "EURES parses without an LLM"


# ---------------------------------------------------------------------------
# C3 caps and C7 cost model
# ---------------------------------------------------------------------------


def test_caps_can_express_the_target():
    """FR-186: 200 pages and 200 companies cannot hold 7,500 companies."""
    assert planning.DEFAULT_CAPS["max_pages"] == 10_000
    assert planning.DEFAULT_CAPS["max_companies"] == 8_000
    assert planning.DEFAULT_CAPS["max_pages_per_source"] == 20
    assert planning.DEFAULT_CAPS["max_duration_seconds"] == 4 * 3600


def test_a_plan_of_one_page_items_is_not_scaled_away():
    """FR-186: the page budget shortens a plan; it must not empty it."""
    planned = [
        planning.PlannedSource(adapter_key="ats.greenhouse", native_query={"slug": f"b{n}"})
        for n in range(7_500)
    ]
    budget = planning.allocate_pages(planned, planning.DEFAULT_CAPS["max_pages"])

    assert budget["granted"] == 7_500
    assert all(item.estimated_pages == 1 for item in planned)


def test_a_deterministic_source_costs_no_tokens_and_one_second_of_overhead(
    monkeypatch, request
):
    """C7: pricing a JSON board like an HTML page rejected a correct plan.

    7,500 boards at 8 s and 2,900 tokens per page displayed 16.7 hours and
    18.75 M tokens on the review screen, which tripped the budget check before
    the campaign could start.
    """
    # The rates otherwise come from the developer's .env, where a free-tier
    # endpoint legitimately sets them to 0 - and then the "an HTML page pays"
    # assertion below is a statement about that machine's config, not the code.
    # The finaliser, rather than a line at the end of the body: a failing assert
    # would otherwise leave the patched rates cached in `get_settings` for every
    # test that runs after this one.
    monkeypatch.setenv("DREAMJOB_LLM_COST_PER_1M_INPUT_EUR", "0.25")
    monkeypatch.setenv("DREAMJOB_LLM_COST_PER_1M_OUTPUT_EUR", "1.00")
    get_settings.cache_clear()
    request.addfinalizer(get_settings.cache_clear)
    board = {"adapter_key": "ats.greenhouse", "access_method": "api", "rate_limit_rps": 0.5}
    html = {"adapter_key": "board.somewhere", "access_method": "http", "rate_limit_rps": 0.5}

    seconds, cost = planning.estimate(board, 7_500)
    assert cost == 0.0
    assert seconds == 7_500 * 3, "2 s of pacing plus 1 s of API overhead"
    assert seconds < 16.7 * 3600

    html_seconds, html_cost = planning.estimate(html, 7_500)
    assert html_cost > 0.0, "an HTML page still pays for the extraction it triggers"
    assert html_seconds > seconds
    assert planning.is_deterministic("board.eures")
    assert not planning.is_deterministic("board.somewhere")


def test_the_model_is_never_asked_to_rewrite_an_evidenced_query(db, tmp_path, monkeypatch):
    """Section 6.7: a hallucinated slug is a 404 that gets stored and re-probed."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _ats_catalogue("greenhouse")
    _registry(monkeypatch, tmp_path, [{"vendor": "greenhouse", "slug": "acme"}])

    class _RecordingLLM:
        def __init__(self):
            self.calls = 0

        class budget:  # noqa: N801 - mirrors LLMClient.budget
            @staticmethod
            def should_degrade() -> bool:
                return False

        def complete_json(self, *a, **kw):
            self.calls += 1
            return {"plans": []}

    llm = _RecordingLLM()
    summary = planning.generate_plan(campaign_id, seeker, use_llm=True, llm=llm)

    assert llm.calls == 0, "a board slug is evidence, not a translation problem"
    assert len(_items(summary, "ats.greenhouse")) == 1
    assert summary["totals"]["estimated_cost_eur"] == 0.0


def test_the_network_crawl_is_not_widened_by_the_campaign_cap(db):
    """FR-165: the LinkedIn crawl is capped on its own terms, not on max_companies."""
    plan = planning.linkedin_network_plan(
        adapter_key="linkedin_network",
        keywords=["data engineer"],
        employers=["Acme"],
        schools=["Ghent"],
        locations=["Gent"],
        caps=dict(planning.DEFAULT_CAPS),
    )

    assert plan.native_query["max_companies"] <= 4 * planning.DEFAULT_NETWORK_CAPS["max_companies"]
    assert plan.native_query["max_profiles"] <= 4 * planning.DEFAULT_NETWORK_CAPS["max_profiles"]


def test_the_network_plan_uses_its_page_budget(db):
    """FR-165: 100 profiles at ~10 a page is ten pages, not one per title.

    One page per title reached ~50 profiles and stopped, so the profile cap the
    campaign asked for was never reached.
    """
    plan = planning.linkedin_network_plan(
        adapter_key="linkedin_network",
        keywords=["data engineer", "platform engineer"],
        employers=["Acme"],
        schools=["Ghent"],
        locations=["Gent"],
        caps={**planning.DEFAULT_CAPS, "max_people": 100},
    )

    urls = plan.native_query["search_urls"]
    assert len(urls) > 2, "more than one page per title"
    assert any("&page=2" in url for url in urls)
    assert plan.estimated_pages == len(urls)


# ---------------------------------------------------------------------------
# The review screen (FR-163)
# ---------------------------------------------------------------------------


def test_the_plan_view_aggregates_by_adapter_and_pages_the_rows(db, tmp_path, monkeypatch):
    """A Campaign A plan is thousands of rows; no screen renders them all."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _ats_catalogue("greenhouse")
    _registry(
        monkeypatch, tmp_path,
        [{"vendor": "greenhouse", "slug": f"board-{n:03d}"} for n in range(250)],
    )

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    assert summary["totals"]["sources"] == 250
    assert summary["items_page"]["total"] == 250
    assert len(summary["items"]) == planning.PLAN_PAGE_SIZE
    assert summary["items_page"]["has_more"] is True

    board = next(r for r in summary["by_adapter"] if r["adapter_key"] == "ats.greenhouse")
    assert board["targets"] == 250
    assert board["estimated_pages"] == 250
    assert board["estimated_cost_eur"] == 0.0
    assert board["statuses"] == {"planned": 250}

    second = planning.plan_summary(campaign_id, seeker, offset=200)
    assert len(second["items"]) == 50
    assert second["items_page"]["has_more"] is False
    first_ids = {i["id"] for i in summary["items"]}
    assert not first_ids & {i["id"] for i in second["items"]}


# ---------------------------------------------------------------------------
# The registry file and the DR-101 identity
# ---------------------------------------------------------------------------


def test_a_missing_registry_is_not_an_error(tmp_path):
    assert discovery.load_board_registry(tmp_path / "nothing-here.json") == []


def test_the_registry_is_read_defensively(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps(
            {
                "boards": [
                    {"vendor": "Greenhouse", "slug": "/acme/", "name": "Acme"},
                    {"vendor": "greenhouse", "slug": "acme"},          # same board twice
                    {"vendor": "lever", "slug": "dead", "live": False},
                    {"vendor": "", "slug": "nameless"},
                    "not a row",
                ]
            }
        ),
        encoding="utf-8",
    )

    boards = discovery.load_board_registry(path)

    assert boards == [
        {"ats_vendor": "greenhouse", "slug": "acme", "name": "Acme", "country": "",
         "source": "board_registry"}
    ]

    path.write_text("{ this is not json", encoding="utf-8")
    assert discovery.load_board_registry(path) == []


def test_a_board_is_a_company_identity(db):
    """DR-101 (migration 091): two rows on one board are one employer twice."""
    insert_row(
        "company",
        {"normalised_name": "acme", "name": "Acme NV", "ats_vendor": "greenhouse",
         "ats_slug": "acme", "collected_at": utcnow()},
    )
    with pytest.raises(sqlite3.IntegrityError):
        insert_row(
            "company",
            {"normalised_name": "acme bv", "name": "Acme BV", "ats_vendor": "greenhouse",
             "ats_slug": "acme", "collected_at": utcnow()},
        )
    # A company with no board is not "the company with the NULL board".
    for name in ("one", "two"):
        insert_row("company", {"normalised_name": name, "name": name, "collected_at": utcnow()})
