"""Remotive adapter: field mapping, attribution, filtering and the stated-empty path.

The fixture is a trimmed payload in the shape remotive.com/api/remote-jobs
answers with.  Nothing here touches the network: the egress layer is a stub
that serves the payload and records what was asked of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.base import PlanItem, RawRecord, all_adapters, get_adapter
from dreamjob.adapters.jobboards.remotive import CACHE_SECONDS, RemotiveAdapter
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS, SourceUnavailable
from dreamjob.db.connection import query_one

PAYLOAD = {
    "0-legal-notice": "Remotive API Legal Notice",
    "job-count": 2,
    "jobs": [
        {
            "id": 1934521,
            "url": "https://remotive.com/remote-jobs/software-dev/senior-python-engineer-1934521",
            "title": "Senior Python Engineer",
            "company_name": "Acme Data",
            "company_logo": "https://remotive.com/job/1934521/logo",
            "category": "Software Development",
            "tags": ["python", "django", "aws"],
            "job_type": "full_time",
            "publication_date": "2026-09-30T08:15:02",
            "candidate_required_location": "Europe",
            "salary": "€70,000 - €90,000",
            "description": (
                "<p>We build data tools.</p><h3>Requirements</h3>"
                "<ul><li>Python and Django</li><li>PostgreSQL</li></ul>"
            ),
        },
        {
            "id": 1934600,
            "url": "https://remotive.com/remote-jobs/software-dev/frontend-developer-1934600",
            "title": "Frontend Developer",
            "company_name": "Pixel Co",
            "category": "Software Development",
            "tags": ["react"],
            "job_type": "contract",
            "publication_date": "2026-09-29T10:00:00",
            "candidate_required_location": "USA",
            "salary": "",
            "description": "<p>React and TypeScript.</p>",
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


def record(payload: dict | None = None, **meta: Any) -> RawRecord:
    return RawRecord(
        url="https://remotive.com/api/remote-jobs?category=software-dev",
        content=json.dumps(payload or PAYLOAD),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"keywords": [], "title_filter": False, **meta},
    )


def test_parse_maps_the_remotive_fields():
    row = RemotiveAdapter().parse(record())[0]
    assert row["title"] == "Senior Python Engineer"
    assert row["company_name_raw"] == "Acme Data"
    assert row["function_family"] == "Software Development"
    assert row["location"] == "Europe"
    assert row["work_arrangement"] == "remote"
    assert row["contract_type"] == "permanent"            # job_type full_time
    assert row["fte_percentage"] == 100
    assert (row["salary_min"], row["salary_max"], row["salary_currency"]) == (
        70000.0, 90000.0, "EUR"
    )
    assert row["posted_at"] == "2026-09-30T08:15:02+00:00"
    assert "python" in row["required_skills"]
    assert "<" not in row["description"]


def test_every_record_links_back_to_remotive():
    """The terms: link back to the posting on Remotive and name it as the source."""
    for row in RemotiveAdapter().parse(record()):
        assert row["source_url"].startswith("https://remotive.com/remote-jobs/")
        assert row["application_target"] == row["source_url"]
    notes = RemotiveAdapter.legal_notes.lower()
    assert "link back" in notes and "remotive as the source" in notes


def test_normalise_writes_only_vacancy_columns():
    adapter = RemotiveAdapter()
    raw = record()
    for parsed in adapter.parse(raw):
        rec = adapter.normalise(parsed, raw)
        assert rec is not None and rec.entity_type == "vacancy"
        assert set(rec.data) <= VACANCY_COLUMNS
        assert rec.data["source_adapter"] == "board.remotive"
        assert rec.data["access_method"] == "api"
        assert rec.data["dedup_key"]


def test_unstated_salary_stays_empty():
    row = RemotiveAdapter().parse(record())[1]
    assert row["salary_min"] is None and row["salary_currency"] is None


async def test_one_cached_category_request_and_client_side_filter():
    egress = StubEgress(json.dumps(PAYLOAD))
    adapter = RemotiveAdapter(egress)
    records = await adapter.run(
        PlanItem("board.remotive", {"page": 1, "keywords": ["python"], "title_filter": True})
    )
    assert [url for url, _ in egress.calls] == [
        "https://remotive.com/api/remote-jobs?category=software-dev"
    ]
    assert egress.calls[0][1]["max_age"] == CACHE_SECONDS     # four answers a day at most
    assert [r.data["title"] for r in records] == ["Senior Python Engineer"]


async def test_without_title_filter_every_row_is_kept():
    adapter = RemotiveAdapter(StubEgress(json.dumps(PAYLOAD)))
    records = await adapter.run(PlanItem("board.remotive", {"keywords": ["python"]}))
    assert len(records) == 2


async def test_an_empty_jobs_list_is_a_stated_empty_answer():
    adapter = RemotiveAdapter(StubEgress(json.dumps({"job-count": 0, "jobs": []})))
    assert await adapter.run(PlanItem("board.remotive", {"page": 1})) == []
    assert adapter.stated_empty == 1
    assert adapter.extraction_rate is None, "an empty board is not a broken parser"


async def test_a_body_that_is_not_json_fails_the_item():
    adapter = RemotiveAdapter(StubEgress("<html>blocked</html>"))
    with pytest.raises(SourceUnavailable):
        await adapter.run(PlanItem("board.remotive", {"page": 1}))


def test_plan_is_one_item_and_the_source_is_catalogued():
    items = RemotiveAdapter().plan(
        {"job_content": {"target_titles": ["Backend Engineer"]}}, {}, {}
    )
    assert len(items) == 1 and items[0].native_query["keywords"] == ["Backend Engineer"]
    load_all()
    assert "board.remotive" in all_adapters()
    row = query_one(
        "SELECT tos_status, legal_notes, coverage_countries FROM source_catalogue "
        "WHERE adapter_key = ?",
        ("board.remotive",),
    )
    # docs/Data_Gathering_Plan.md rule 3 names "Remotive's /api/*" among the
    # paths not to fetch through the egress layer, as robots-disallowed.  The
    # adapter therefore declares itself restricted and ack-gated, like
    # board.jobat: anything else tells the source dashboard the opposite of the
    # project's own finding.
    assert row is not None and row["tos_status"] == "restricted" and row["legal_notes"]
    # Ack-gated means not enabled until an operator acknowledges it, so the
    # planner cannot budget items for a source the robots gate would refuse.
    adapter = get_adapter("board.remotive")
    assert adapter.requires_ack is True
    assert adapter.is_enabled() is False
