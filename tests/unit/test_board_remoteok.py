"""Remote OK adapter: the legal element, field mapping, attribution and filtering.

The fixture is a trimmed payload in the shape remoteok.com/api answers with:
an array whose first element is the legal notice, not a job.  The egress layer
is a stub; nothing here touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.base import PlanItem, RawRecord, all_adapters, get_adapter
from dreamjob.adapters.jobboards.remoteok import RemoteOkAdapter
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS, SourceUnavailable
from dreamjob.db.connection import query_one

LEGAL = {
    "last_updated": 1791100000,
    "legal": "API Terms of Service: Please link back (with follow, no nofollow!) to the URL "
             "on Remote OK and mention Remote OK as a source.",
}
PAYLOAD = [
    LEGAL,
    {
        "slug": "remote-staff-backend-engineer-globex-1129001",
        "id": "1129001",
        "epoch": 1791050000,
        "date": "2026-10-03T09:00:00+00:00",
        "company": "Globex",
        "position": "Staff Backend Engineer",
        "tags": ["golang", "kubernetes", "full time"],
        "description": "<p>Go services on <b>Kubernetes</b>.</p>",
        "location": "Worldwide",
        "salary_min": 140000,
        "salary_max": 180000,
        "apply_url": "https://boards.greenhouse.io/globex/jobs/1129001",
        "url": "https://remoteOK.com/remote-jobs/remote-staff-backend-engineer-globex-1129001",
    },
    {
        "slug": "remote-customer-support-initech-1129002",
        "id": "1129002",
        "epoch": 1791040000,
        "date": "2026-10-03T06:00:00+00:00",
        "company": "Initech",
        "position": "Customer Support Specialist",
        "tags": ["support"],
        "description": "<p>Help customers.</p>",
        "location": "",
        "salary_min": 0,
        "salary_max": 0,
        "apply_url": "",
        "url": "https://remoteOK.com/remote-jobs/remote-customer-support-initech-1129002",
    },
]


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


def record(payload: list | None = None) -> RawRecord:
    return RawRecord(
        url="https://remoteok.com/api",
        content=json.dumps(PAYLOAD if payload is None else payload),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"keywords": [], "title_filter": False},
    )


def test_the_legal_element_is_not_a_job():
    rows = RemoteOkAdapter().parse(record())
    assert [r["title"] for r in rows] == ["Staff Backend Engineer", "Customer Support Specialist"]


def test_parse_maps_the_remoteok_fields():
    row = RemoteOkAdapter().parse(record())[0]
    assert row["company_name_raw"] == "Globex"
    assert row["function_family"] == "golang"
    assert row["work_arrangement"] == "remote"
    assert row["contract_type"] == "permanent"             # tag "full time"
    assert (row["salary_min"], row["salary_max"], row["salary_currency"]) == (
        140000.0, 180000.0, "USD"
    )
    assert row["posted_at"] == "2026-10-03T09:00:00+00:00"
    assert row["description"] == "Go services on Kubernetes."
    # The employer's ATS link is the route to apply ...
    assert row["application_channel"] == "ats_form"
    assert row["application_target"].startswith("https://boards.greenhouse.io/")


def test_the_source_is_always_the_remoteok_posting():
    """... while the Remote OK page stays the source: the terms' link back."""
    for row in RemoteOkAdapter().parse(record()):
        assert row["source_url"].lower().startswith("https://remoteok.com/remote-jobs/")
    support = RemoteOkAdapter().parse(record())[1]
    assert support["application_target"] == support["source_url"]
    assert "link back" in RemoteOkAdapter.legal_notes.lower()


def test_zero_salary_means_not_stated():
    row = RemoteOkAdapter().parse(record())[1]
    assert row["salary_min"] is None and row["salary_currency"] is None


def test_normalise_writes_only_vacancy_columns():
    adapter = RemoteOkAdapter()
    raw = record()
    for parsed in adapter.parse(raw):
        rec = adapter.normalise(parsed, raw)
        assert rec is not None and set(rec.data) <= VACANCY_COLUMNS
        assert rec.data["source_adapter"] == "board.remoteok"


async def test_one_request_and_client_side_title_filter():
    egress = StubEgress(json.dumps(PAYLOAD))
    records = await RemoteOkAdapter(egress).run(
        PlanItem("board.remoteok", {"keywords": ["backend"], "title_filter": True})
    )
    assert egress.calls == ["https://remoteok.com/api"]
    assert [r.data["title"] for r in records] == ["Staff Backend Engineer"]


async def test_a_feed_holding_only_the_legal_notice_is_stated_empty():
    adapter = RemoteOkAdapter(StubEgress(json.dumps([LEGAL])))
    assert await adapter.run(PlanItem("board.remoteok", {"page": 1})) == []
    assert adapter.stated_empty == 1
    assert adapter.extraction_rate is None


async def test_an_object_instead_of_the_array_fails_the_item():
    adapter = RemoteOkAdapter(StubEgress(json.dumps({"error": "blocked"})))
    with pytest.raises(SourceUnavailable):
        await adapter.run(PlanItem("board.remoteok", {"page": 1}))


def test_registered_and_catalogued():
    load_all()
    assert "board.remoteok" in all_adapters()
    row = query_one(
        "SELECT tos_status, legal_notes FROM source_catalogue WHERE adapter_key = ?",
        ("board.remoteok",),
    )
    assert row is not None and row["tos_status"] == "permitted" and row["legal_notes"]
    assert get_adapter("board.remoteok").is_enabled() is True
    assert RemoteOkAdapter.coverage_countries == []
