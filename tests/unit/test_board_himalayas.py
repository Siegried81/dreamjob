"""Himalayas adapter: field mapping, attribution, offset paging and stated empty.

The fixture is a trimmed payload in the shape himalayas.app/jobs/api answers
with.  The egress layer is a stub; nothing here touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dreamjob.adapters import load_all
from dreamjob.adapters.base import PlanItem, RawRecord, all_adapters, get_adapter
from dreamjob.adapters.jobboards.himalayas import (
    MAX_PAGES_PER_ITEM,
    PAGE_SIZE,
    HimalayasAdapter,
)
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS
from dreamjob.db.connection import query_one

PAYLOAD = {
    "updatedAt": 1791100000,
    "offset": 0,
    "limit": 20,
    "totalCount": 2,
    "jobs": [
        {
            "title": "Machine Learning Engineer",
            "excerpt": "Ship models.",
            "companyName": "Hooli",
            "employmentType": "Full Time",
            "minSalary": 90000,
            "maxSalary": 120000,
            "currency": "EUR",
            "seniority": ["Mid-level", "Senior"],
            "locationRestrictions": ["Netherlands"],
            "categories": ["Machine-Learning-Engineer"],
            "description": "<p>PyTorch models in production.</p>",
            "pubDate": 1791000000,
            "applicationLink": "https://himalayas.app/companies/hooli/jobs/machine-learning-engineer",
            "guid": "https://himalayas.app/companies/hooli/jobs/machine-learning-engineer",
        },
        {
            "title": "Account Executive",
            "companyName": "Pied Piper",
            "employmentType": "Contractor",
            "locationRestrictions": ["Germany", "France"],
            "categories": [],
            "description": "<p>Sell.</p>",
            "pubDate": 1790900000,
            "guid": "https://himalayas.app/companies/pied-piper/jobs/account-executive",
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
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        return StubResponse(url=url, text=self.body)


def record() -> RawRecord:
    return RawRecord(
        url="https://himalayas.app/jobs/api?limit=20&offset=0",
        content=json.dumps(PAYLOAD),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"keywords": [], "title_filter": False, "page": 1},
    )


def test_parse_maps_the_himalayas_fields():
    row = HimalayasAdapter().parse(record())[0]
    assert row["company_name_raw"] == "Hooli"
    assert row["function_family"] == "Machine Learning Engineer"
    assert row["seniority"] == "Mid-level, Senior"
    assert row["location"] == "Netherlands" and row["country"] == "NL"
    assert row["work_arrangement"] == "remote"
    assert row["contract_type"] == "permanent"
    assert row["fte_percentage"] == 100
    assert (row["salary_min"], row["salary_max"], row["salary_currency"]) == (
        90000.0, 120000.0, "EUR"
    )
    assert row["posted_at"] == "2026-10-03T04:00:00+00:00"        # epoch seconds


def test_a_list_of_eligible_countries_is_not_a_country():
    row = HimalayasAdapter().parse(record())[1]
    assert row["location"] == "Germany, France"
    assert row["country"] is None
    assert row["contract_type"] == "freelance"
    assert row["salary_min"] is None and row["salary_currency"] is None


def test_every_record_links_back_to_himalayas():
    for row in HimalayasAdapter().parse(record()):
        assert row["source_url"].startswith("https://himalayas.app/companies/")
        assert row["application_target"] == row["source_url"]
    assert "link back" in HimalayasAdapter.legal_notes.lower()


def test_normalise_writes_only_vacancy_columns():
    adapter = HimalayasAdapter()
    raw = record()
    for parsed in adapter.parse(raw):
        rec = adapter.normalise(parsed, raw)
        assert rec is not None and set(rec.data) <= VACANCY_COLUMNS
        assert rec.data["source_adapter"] == "board.himalayas"


async def test_the_requested_page_becomes_an_offset_and_the_filter_applies():
    egress = StubEgress(json.dumps({**PAYLOAD, "totalCount": 500}))
    records = await HimalayasAdapter(egress).run(
        PlanItem("board.himalayas", {"page": 3, "keywords": ["machine learning"],
                                     "title_filter": True})
    )
    assert egress.calls == [f"https://himalayas.app/jobs/api?limit={PAGE_SIZE}&offset=40"]
    assert [r.data["title"] for r in records] == ["Machine Learning Engineer"]


async def test_the_walk_stops_at_total_count():
    egress = StubEgress(json.dumps(PAYLOAD))      # totalCount 2: one page holds it all
    await HimalayasAdapter(egress).run(PlanItem("board.himalayas", {"page": 1, "pages": 5}))
    assert len(egress.calls) == 1


def test_the_plan_is_bounded_whatever_the_caps():
    items = HimalayasAdapter().plan({}, {}, {"max_pages_per_source": 10_000})
    assert len(items) == 1 and "page" not in items[0].native_query
    assert items[0].estimated_pages == MAX_PAGES_PER_ITEM


async def test_an_empty_jobs_list_is_a_stated_empty_answer():
    adapter = HimalayasAdapter(StubEgress(json.dumps({"totalCount": 0, "jobs": []})))
    assert await adapter.run(PlanItem("board.himalayas", {"page": 1})) == []
    assert adapter.stated_empty == 1
    assert adapter.extraction_rate is None


def test_registered_and_catalogued():
    load_all()
    assert "board.himalayas" in all_adapters()
    row = query_one(
        "SELECT tos_status, legal_notes FROM source_catalogue WHERE adapter_key = ?",
        ("board.himalayas",),
    )
    assert row is not None and row["tos_status"] == "permitted" and row["legal_notes"]
    assert get_adapter("board.himalayas").is_enabled() is True
