"""Adzuna adapter: inert without a key, keyed requests, field mapping, attribution.

The fixture is a trimmed payload in the shape api.adzuna.com/v1/api/jobs
answers with.  The egress layer is a stub; nothing here touches the network,
and the key used here is a placeholder set through the environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.base import (
    PlanItem,
    RawRecord,
    adapter_unavailable_reason,
    all_adapters,
    get_adapter,
)
from dreamjob.adapters.jobboards.adzuna import MAX_KEYWORDS, AdzunaAdapter
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS, UnusableQuery
from dreamjob.config import get_settings
from dreamjob.db.connection import query_one

PAYLOAD = {
    "count": 2,
    "mean": 61000,
    "results": [
        {
            "id": "4900000001",
            "title": "<strong>Python</strong> Developer",
            "description": "Backend services in Python and Django, hybrid in Ghent...",
            "created": "2026-10-02T11:20:00Z",
            "redirect_url": "https://www.adzuna.be/details/4900000001?utm_medium=api",
            "company": {"display_name": "Initrode NV"},
            "location": {"display_name": "Gent, Oost-Vlaanderen", "area": ["Belgium", "Gent"]},
            "category": {"tag": "it-jobs", "label": "IT Jobs"},
            "contract_type": "permanent",
            "contract_time": "full_time",
            "salary_min": 48000,
            "salary_max": 60000,
            "salary_is_predicted": "0",
            "latitude": 51.05,
            "longitude": 3.72,
        },
        {
            "id": "4900000002",
            "title": "DevOps Engineer",
            "description": "Kubernetes on Azure.",
            "created": "2026-10-01T09:00:00Z",
            "redirect_url": "https://www.adzuna.be/details/4900000002",
            "company": {"display_name": "Vandelay BV"},
            "location": {"display_name": "Brussels"},
            "category": {"tag": "it-jobs", "label": "IT Jobs"},
            "contract_type": "contract",
            "contract_time": "full_time",
            "salary_min": 55000,
            "salary_max": 55000,
            "salary_is_predicted": "1",
        },
    ],
}


@dataclass
class StubResponse:
    url: str
    text: str
    status_code: int = 200
    raw_document_id: str | None = "raw-1"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class StubEgress:
    body: str
    calls: list[tuple[str, dict]] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append((url, kwargs))
        return StubResponse(url=url, text=self.body)


@pytest.fixture()
def keyed(monkeypatch):
    monkeypatch.setenv("DREAMJOB_ADZUNA_APP_ID", "test-id")
    monkeypatch.setenv("DREAMJOB_ADZUNA_APP_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def unkeyed(monkeypatch):
    # Empty values override anything a local .env may hold.
    monkeypatch.setenv("DREAMJOB_ADZUNA_APP_ID", "")
    monkeypatch.setenv("DREAMJOB_ADZUNA_APP_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def record() -> RawRecord:
    return RawRecord(
        url="https://api.adzuna.com/v1/api/jobs/be/search/1?what=python",
        content=json.dumps(PAYLOAD),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"keywords": [], "title_filter": False, "country": "BE"},
    )


def test_without_a_key_it_is_inert_and_says_why(unkeyed):
    adapter = AdzunaAdapter()
    assert adapter.available() is False
    reason = adapter_unavailable_reason(adapter)
    assert reason and "DREAMJOB_ADZUNA_APP_ID" in reason
    assert adapter.plan({"location": {"countries": ["BE"]}}, {}, {}) == []


async def test_without_a_key_no_request_is_issued(unkeyed):
    egress = StubEgress(json.dumps(PAYLOAD))
    with pytest.raises(UnusableQuery, match="not configured"):
        await AdzunaAdapter(egress).run(PlanItem("board.adzuna", {"country": "BE"}))
    assert egress.calls == []


def test_plan_is_one_item_per_served_country(keyed):
    items = AdzunaAdapter().plan(
        {"location": {"countries": ["BE", "NL", "LU"]},
         "job_content": {"target_titles": ["Python Developer"]}},
        {}, {"max_pages_per_source": 3},
    )
    assert [i.native_query["country"] for i in items] == ["BE", "NL"]   # LU is not served
    assert items[0].native_query["keywords"] == ["Python Developer"]
    assert items[0].estimated_pages == 3


async def test_the_key_travels_as_parameters_never_in_the_recorded_url(keyed):
    egress = StubEgress(json.dumps(PAYLOAD))
    adapter = AdzunaAdapter(egress)
    records = await adapter.run(
        PlanItem("board.adzuna", {"country": "BE", "page": 2,
                                  "keywords": ["python", "devops", "data", "java"]})
    )
    assert len(egress.calls) == MAX_KEYWORDS
    url, kwargs = egress.calls[0]
    assert url == "https://api.adzuna.com/v1/api/jobs/be/search/2"
    assert kwargs["params"]["app_id"] == "test-id"
    assert kwargs["params"]["what"] == "python"
    assert kwargs["params"]["category"] == "it-jobs"
    assert "test-key" not in url
    assert records and all("test-key" not in r.data["source_url"] for r in records)


def test_parse_maps_the_adzuna_fields():
    row = AdzunaAdapter().parse(record())[0]
    assert row["title"] == "Python Developer"
    assert row["company_name_raw"] == "Initrode NV"
    assert row["function_family"] == "IT Jobs"
    assert row["location"] == "Gent, Oost-Vlaanderen" and row["country"] == "BE"
    assert (row["latitude"], row["longitude"]) == (51.05, 3.72)
    assert row["work_arrangement"] == "hybrid"
    assert row["contract_type"] == "permanent" and row["fte_percentage"] == 100
    assert (row["salary_min"], row["salary_max"], row["salary_currency"]) == (
        48000.0, 60000.0, "EUR"
    )
    assert row["posted_at"] == "2026-10-02T11:20:00+00:00"


def test_a_predicted_salary_is_not_stored_and_contract_is_not_read_as_permanent():
    row = AdzunaAdapter().parse(record())[1]
    assert row["salary_min"] is None and row["salary_currency"] is None
    assert row["contract_type"] != "permanent", "'full_time' must not override 'contract'"


def test_every_record_goes_through_adzunas_redirect_url():
    for row in AdzunaAdapter().parse(record()):
        assert row["source_url"].startswith("https://www.adzuna.be/details/")
        assert row["application_target"] == row["source_url"]
    assert "adzuna" in AdzunaAdapter.legal_notes.lower()
    assert "redirect_url" in AdzunaAdapter.legal_notes


def test_normalise_writes_only_vacancy_columns():
    adapter = AdzunaAdapter()
    raw = record()
    for parsed in adapter.parse(raw):
        rec = adapter.normalise(parsed, raw)
        assert rec is not None and set(rec.data) <= VACANCY_COLUMNS
        assert rec.data["source_adapter"] == "board.adzuna"


async def test_an_empty_result_list_is_a_stated_empty_answer(keyed):
    adapter = AdzunaAdapter(StubEgress(json.dumps({"count": 0, "results": []})))
    assert await adapter.run(PlanItem("board.adzuna", {"country": "BE", "page": 1})) == []
    assert adapter.stated_empty == 1
    assert adapter.extraction_rate is None


async def test_the_title_filter_applies_when_asked(keyed):
    adapter = AdzunaAdapter(StubEgress(json.dumps(PAYLOAD)))
    records = await adapter.run(
        PlanItem("board.adzuna", {"country": "BE", "keywords": ["devops"],
                                  "title_filter": True})
    )
    assert [r.data["title"] for r in records] == ["DevOps Engineer"]


def test_registered_and_catalogued_with_its_coverage():
    load_all()
    assert "board.adzuna" in all_adapters()
    row = query_one(
        "SELECT tos_status, legal_notes, coverage_countries FROM source_catalogue "
        "WHERE adapter_key = ?",
        ("board.adzuna",),
    )
    assert row is not None and row["tos_status"] == "permitted" and row["legal_notes"]
    adapter = get_adapter("board.adzuna")
    assert adapter.covers_country("BE") and adapter.covers_country("GB")
    assert not adapter.covers_country("LU")
