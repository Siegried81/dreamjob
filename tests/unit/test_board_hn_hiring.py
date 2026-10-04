"""Hacker News "Who is hiring?" adapter: header parsing, attribution, the two requests.

The fixtures are trimmed payloads in the shapes the HN Search API answers with
(``search_by_date`` hits and an ``items/{id}`` thread tree).  The egress layer
is a stub; nothing here touches the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.base import PlanItem, RawRecord, all_adapters, get_adapter
from dreamjob.adapters.jobboards.hn_hiring import HnHiringAdapter, header_fields
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS
from dreamjob.db.connection import query_one

SEARCH = {
    "hits": [
        {"objectID": "45400002", "title": "Ask HN: Who wants to be hired? (October 2026)"},
        {"objectID": "45400001", "title": "Ask HN: Who is hiring? (October 2026)"},
        {"objectID": "45100001", "title": "Ask HN: Who is hiring? (September 2026)"},
    ]
}
THREAD = {
    "id": 45400001,
    "title": "Ask HN: Who is hiring? (October 2026)",
    "type": "story",
    "children": [
        {
            "id": 45400101,
            "author": "acme_cto",
            "created_at": "2026-10-01T15:02:11.000Z",
            "type": "comment",
            "text": (
                "Acme Robotics | Senior Backend Engineer | Berlin, Germany | ONSITE | "
                "Full-time | €80,000 - €100,000<p>We build warehouse robots in Python "
                "and Rust.<p>Apply: https:&#x2F;&#x2F;acme.example&#x2F;jobs"
            ),
            "children": [
                {"id": 45400201, "author": "curious", "type": "comment",
                 "text": "Is this open to juniors?", "children": []}
            ],
        },
        {
            "id": 45400102,
            "author": "globex_hr",
            "created_at": "2026-10-01T16:00:00.000Z",
            "type": "comment",
            "text": (
                "Globex (https:&#x2F;&#x2F;globex.example) | REMOTE (US) | "
                "Data Scientist, ML Engineer | $150k-$190k<p>Pandas, PyTorch."
            ),
            "children": [],
        },
        {
            "id": 45400103,
            "author": "someone",
            "created_at": "2026-10-01T17:00:00.000Z",
            "type": "comment",
            "text": "Great thread, thanks for running it every month!",
            "children": [],
        },
        {"id": 45400104, "author": None, "type": "comment", "text": None, "children": []},
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
    pages: dict[str, str]
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        for fragment, body in self.pages.items():
            if fragment in url:
                return StubResponse(url=url, text=body)
        return StubResponse(url=url, text="", status_code=404)


def record(thread: dict | None = None, **meta: Any) -> RawRecord:
    return RawRecord(
        url="https://hn.algolia.com/api/v1/items/45400001",
        content=json.dumps(thread or THREAD),
        content_type="application/json",
        raw_document_id="raw-1",
        meta={"keywords": [], "title_filter": False, **meta},
    )


def test_the_header_line_is_split_into_fields_by_shape():
    fields = header_fields(
        "Acme Robotics | Senior Backend Engineer | Berlin, Germany | ONSITE | €80,000 - €100,000"
    )
    assert fields["company"] == "Acme Robotics"
    assert fields["role"] == "Senior Backend Engineer"
    assert fields["location"] == "Berlin, Germany"
    assert fields["salary"] == (80000.0, 100000.0, "EUR")


def test_fields_are_found_out_of_order_and_urls_are_dropped():
    fields = header_fields("Globex (https://globex.example) | REMOTE (US) | Data Scientist")
    assert fields["company"] == "Globex"
    assert fields["role"] == "Data Scientist"
    assert fields["location"] == "REMOTE (US)"          # an area, no place given


def test_a_line_without_pipes_is_not_a_posting():
    assert header_fields("Great thread, thanks!") is None


def test_parse_reads_top_level_postings_only():
    rows = HnHiringAdapter().parse(record())
    assert [r["company_name_raw"] for r in rows] == ["Acme Robotics", "Globex"]
    acme = rows[0]
    assert acme["title"] == "Senior Backend Engineer"
    assert acme["country"] == "DE"
    assert acme["work_arrangement"] == "onsite"
    assert acme["contract_type"] == "permanent"
    assert acme["salary_min"] == 80000.0 and acme["salary_currency"] == "EUR"
    assert acme["posted_at"] == "2026-10-01T15:02:11+00:00"
    assert "warehouse robots" in acme["description"]
    assert rows[1]["work_arrangement"] == "remote"
    assert rows[1]["salary_min"] is None, "'$150k' is not a stated figure we can read"


def test_every_record_links_to_its_hn_comment_and_names_no_author():
    for row in HnHiringAdapter().parse(record()):
        assert row["source_url"].startswith("https://news.ycombinator.com/item?id=4540010")
        assert row["application_target"] == row["source_url"]
        assert "acme_cto" not in json.dumps(row) and "globex_hr" not in json.dumps(row)


def test_normalise_writes_only_vacancy_columns():
    adapter = HnHiringAdapter()
    raw = record()
    for parsed in adapter.parse(raw):
        rec = adapter.normalise(parsed, raw)
        assert rec is not None and set(rec.data) <= VACANCY_COLUMNS
        assert rec.data["source_adapter"] == "board.hn_hiring"


def test_the_title_filter_reads_the_whole_header():
    rows = HnHiringAdapter().parse(record(keywords=["ML Engineer"], title_filter=True))
    assert [r["company_name_raw"] for r in rows] == ["Globex"]


def test_a_thread_whose_comments_carry_no_headers_is_breakage():
    thread = {"id": 1, "children": [{"id": 2, "text": "no pipes here", "children": []}]}
    with pytest.raises(ValueError):
        HnHiringAdapter().parse(record(thread))


async def test_two_requests_find_and_read_the_hiring_thread():
    egress = StubEgress({"search_by_date": json.dumps(SEARCH),
                         "items/45400001": json.dumps(THREAD)})
    records = await HnHiringAdapter(egress).run(PlanItem("board.hn_hiring", {"page": 1}))
    assert egress.calls == [
        "https://hn.algolia.com/api/v1/search_by_date?tags=story%2Cauthor_whoishiring"
        "&hitsPerPage=10",
        "https://hn.algolia.com/api/v1/items/45400001",       # not "Who wants to be hired?"
    ]
    assert len(records) == 2


async def test_an_empty_thread_is_a_stated_empty_answer():
    egress = StubEgress({"search_by_date": json.dumps(SEARCH),
                         "items/45400001": json.dumps({"id": 45400001, "children": []})})
    adapter = HnHiringAdapter(egress)
    assert await adapter.run(PlanItem("board.hn_hiring", {"page": 1})) == []
    assert adapter.stated_empty == 1
    assert adapter.extraction_rate is None


async def test_no_hiring_thread_in_the_search_is_a_stated_empty_answer():
    egress = StubEgress({"search_by_date": json.dumps({"hits": []})})
    adapter = HnHiringAdapter(egress)
    assert await adapter.run(PlanItem("board.hn_hiring", {"page": 1})) == []
    assert adapter.stated_empty == 1
    assert len(egress.calls) == 1


def test_registered_catalogued_and_never_calls_the_llm():
    assert HnHiringAdapter.llm_fallback is False
    assert HnHiringAdapter().llm_extract("text", "https://example.invalid") is None
    load_all()
    assert "board.hn_hiring" in all_adapters()
    row = query_one(
        "SELECT tos_status, legal_notes FROM source_catalogue WHERE adapter_key = ?",
        ("board.hn_hiring",),
    )
    assert row is not None and row["tos_status"] == "permitted" and row["legal_notes"]
    assert get_adapter("board.hn_hiring").is_enabled() is True
