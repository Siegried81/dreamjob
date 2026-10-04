"""Jobicy adapter: field mapping, attribution, the request it sends, stated empty.

The fixture is a trimmed payload in the shape jobicy.com/api/v2/remote-jobs
answers with.  The egress layer is a stub; nothing here touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dreamjob.adapters import load_all
from dreamjob.adapters.base import PlanItem, RawRecord, all_adapters, get_adapter
from dreamjob.adapters.jobboards.jobicy import CACHE_SECONDS, JobicyAdapter
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS
from dreamjob.db.connection import query_one

PAYLOAD = {
    "apiVersion": "2",
    "friendlyNotice": "Please link back to Jobicy and credit it as the source.",
    "jobCount": 2,
    "jobs": [
        {
            "id": 118201,
            "url": "https://jobicy.com/jobs/118201-data-engineer",
            "jobSlug": "118201-data-engineer",
            "jobTitle": "Data Engineer &#8211; Streaming",
            "companyName": "Umbrella &amp; Co",
            "jobIndustry": ["Data Science &amp; Analytics"],
            "jobType": ["full-time"],
            "jobGeo": "Germany",
            "jobLevel": "Senior",
            "jobExcerpt": "Kafka pipelines.",
            "jobDescription": "<p>Kafka and Spark pipelines.</p><p>Requirements: Python, SQL</p>",
            "pubDate": "2026-10-02 07:30:00",
            "annualSalaryMin": "75000",
            "annualSalaryMax": "95000",
            "salaryCurrency": "eur",
        },
        {
            "id": 118202,
            "url": "https://jobicy.com/jobs/118202-content-writer",
            "jobTitle": "Content Writer",
            "companyName": "Wordsmiths",
            "jobIndustry": ["Copywriting"],
            "jobType": ["contract"],
            "jobGeo": "Anywhere",
            "jobLevel": "Any",
            "jobDescription": "<p>Write.</p>",
            "pubDate": "2026-10-01 07:30:00",
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


def record() -> RawRecord:
    return RawRecord(
        url="https://jobicy.com/api/v2/remote-jobs?count=50",
        content=json.dumps(PAYLOAD),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"keywords": [], "title_filter": False},
    )


def test_parse_maps_the_jobicy_fields():
    row = JobicyAdapter().parse(record())[0]
    assert row["title"] == "Data Engineer – Streaming"      # entity decoded
    assert row["company_name_raw"] == "Umbrella & Co"
    assert row["function_family"] == "Data Science & Analytics"
    assert row["seniority"] == "Senior"
    assert row["location"] == "Germany" and row["country"] == "DE"
    assert row["work_arrangement"] == "remote"
    assert row["contract_type"] == "permanent"
    assert (row["salary_min"], row["salary_max"], row["salary_currency"]) == (
        75000.0, 95000.0, "EUR"
    )
    assert row["posted_at"] == "2026-10-02T07:30:00+00:00"


def test_any_level_and_missing_salary_stay_empty():
    row = JobicyAdapter().parse(record())[1]
    assert row["seniority"] is None
    assert row["salary_min"] is None and row["salary_currency"] is None


def test_every_record_links_back_to_jobicy():
    for row in JobicyAdapter().parse(record()):
        assert row["source_url"].startswith("https://jobicy.com/jobs/")
        assert row["application_target"] == row["source_url"]
    assert "link back" in JobicyAdapter.legal_notes.lower()


def test_normalise_writes_only_vacancy_columns():
    adapter = JobicyAdapter()
    raw = record()
    for parsed in adapter.parse(raw):
        rec = adapter.normalise(parsed, raw)
        assert rec is not None and set(rec.data) <= VACANCY_COLUMNS
        assert rec.data["source_adapter"] == "board.jobicy"


async def test_one_cached_request_without_guessed_filters():
    egress = StubEgress(json.dumps(PAYLOAD))
    records = await JobicyAdapter(egress).run(
        PlanItem("board.jobicy", {"keywords": ["data engineer"], "title_filter": True})
    )
    assert [url for url, _ in egress.calls] == ["https://jobicy.com/api/v2/remote-jobs?count=50"]
    assert egress.calls[0][1]["max_age"] == CACHE_SECONDS
    assert [r.data["company_name_raw"] for r in records] == ["Umbrella & Co"]


async def test_geo_and_industry_are_sent_only_when_the_plan_names_them():
    egress = StubEgress(json.dumps(PAYLOAD))
    await JobicyAdapter(egress).run(
        PlanItem("board.jobicy", {"geo": "europe", "industry": "dev", "count": 20})
    )
    assert egress.calls[0][0] == (
        "https://jobicy.com/api/v2/remote-jobs?count=20&geo=europe&industry=dev"
    )


async def test_an_empty_jobs_list_is_a_stated_empty_answer():
    adapter = JobicyAdapter(StubEgress(json.dumps({"jobCount": 0, "jobs": []})))
    assert await adapter.run(PlanItem("board.jobicy", {"page": 1})) == []
    assert adapter.stated_empty == 1
    assert adapter.extraction_rate is None


def test_registered_and_catalogued():
    load_all()
    assert "board.jobicy" in all_adapters()
    row = query_one(
        "SELECT tos_status, legal_notes FROM source_catalogue WHERE adapter_key = ?",
        ("board.jobicy",),
    )
    assert row is not None and row["tos_status"] == "permitted" and row["legal_notes"]
    assert get_adapter("board.jobicy").is_enabled() is True
