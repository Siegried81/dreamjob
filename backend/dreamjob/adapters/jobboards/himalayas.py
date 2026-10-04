"""Himalayas adapter - the public Himalayas remote-jobs API (FR-181, FR-261, IR-101).

Endpoint (keyless, documented at himalayas.app/api):

    GET https://himalayas.app/jobs/api?limit=20&offset=0
    -> {"updatedAt", "offset", "limit", "totalCount",
        "jobs": [{"title", "excerpt", "companyName", "employmentType",
                  "minSalary", "maxSalary", "currency", "seniority": [...],
                  "locationRestrictions": [...], "categories": [...],
                  "description" (HTML), "pubDate" (epoch seconds),
                  "applicationLink", "guid"}]}

The terms Himalayas attaches to its API: link back to the job's page on
Himalayas and mention Himalayas as the source; the API may not be used to feed
other job boards (Jooble, Google Jobs, LinkedIn and the like).  IR-101 makes
that binding:

* **Link back.**  ``source_url`` and ``application_target`` are always the
  posting's himalayas.app page (``applicationLink``, else ``guid``).
* **Bounded paging.**  The service answers at most twenty rows a request, so
  the feed is paged with ``offset``.  The page is the collection pipeline's to
  set (see :func:`requested_page`); this adapter reads exactly that page, the
  walk is bounded by ``MAX_PAGES_PER_ITEM`` whatever the caps say, and it stops
  at ``totalCount``.  Keyword filtering is client-side.
* **The terms travel with the data** in ``legal_notes``.

Remote-only.  ``locationRestrictions`` lists the countries a candidate may live
in (empty = worldwide); a single country is stored as ``country``.  The salary
pair is the posted annual range.  ``llm_fallback`` is False: 0 tokens.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from dreamjob.adapters.base import AdapterCapabilities, PlanItem, RawRecord, register_adapter
from dreamjob.adapters.jobboards._json_feed import JsonFeedAdapter
from dreamjob.adapters.vacancy_source import (
    annualise_salary,
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    parse_datetime,
    requested_page,
    split_skills,
)

log = logging.getLogger(__name__)

API = "https://himalayas.app/jobs/api"
#: The service's own ceiling on ``limit``.
PAGE_SIZE = 20
#: Ceiling on one plan item's walk, whatever the caps say.
MAX_PAGES_PER_ITEM = 10


@register_adapter
class HimalayasAdapter(JsonFeedAdapter):
    key = "board.himalayas"
    display_name = "Himalayas"
    coverage_countries: list[str] = []      # remote roles, worldwide
    rate_limit_rps = 0.5
    legal_notes = (
        "Free public API (himalayas.app/jobs/api). Terms: link back to the job's page on "
        "Himalayas and mention Himalayas as the source; the API may not be used to submit "
        "jobs to third-party job boards. Honoured by keeping the himalayas.app posting as "
        "the source and application URL of every record and by a bounded walk of at most "
        f"{MAX_PAGES_PER_ITEM} pages of {PAGE_SIZE} per plan item."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,
        location_filter=False,
        work_arrangement_filter=True,
        pagination=True,
        max_results_per_query=PAGE_SIZE,
    )

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        pages = max(1, min(MAX_PAGES_PER_ITEM, int(caps.get("max_pages_per_source") or 3)))
        # One item for the feed, paged by the pipeline: an item per page would
        # have every one of them overwritten to page 1 (see arbeitnow.plan).
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={
                    "pages": 1,
                    "keywords": keywords_from(directives, composite_profile),
                    "title_filter": bool(caps.get("title_filter")),
                },
                rationale=(
                    f"Himalayas publishes remote jobs as a keyless JSON API, {PAGE_SIZE} "
                    f"per request; {pages} page(s), newest first"
                ),
                estimated_pages=pages,
                estimated_seconds=3 * pages,
                caps={"max_records": PAGE_SIZE * pages},
            )
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        first = requested_page(query) or 1
        pages = max(1, min(MAX_PAGES_PER_ITEM, int(query.get("pages") or 1)))
        records: list[RawRecord] = []
        for page in range(first, min(first + pages, MAX_PAGES_PER_ITEM + 1)):
            offset = (page - 1) * PAGE_SIZE
            url = f"{API}?{urlencode({'limit': PAGE_SIZE, 'offset': offset})}"
            got = await self._get_json(url)
            if got is None:
                break
            result, payload = got
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                self.fetch_outcome.ok -= 1
                self.fetch_outcome.failures.append((url, "no jobs array"))
                break
            records.append(self.json_record(url, result, self.feed_meta(query, page=page)))
            total = payload.get("totalCount")
            if not payload["jobs"] or (isinstance(total, int) and offset + PAGE_SIZE >= total):
                break
        return self.settle(
            records,
            nothing_to_fetch=f"page {first} is beyond the {MAX_PAGES_PER_ITEM}-page bound",
        )

    def parse(self, raw: RawRecord) -> list[dict]:
        rows = json.loads(raw.content).get("jobs") or []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if title and self.wanted(title, raw.meta):
                out.append(self._one(row))
        return out

    def _one(self, row: dict) -> dict[str, Any]:
        description = html_to_text(row.get("description") or row.get("excerpt"))
        countries = [str(c).strip() for c in (row.get("locationRestrictions") or []) if c]
        location = ", ".join(countries)
        employment = str(row.get("employmentType") or "")
        categories = [str(c) for c in (row.get("categories") or []) if c]
        seniority = [str(s) for s in (row.get("seniority") or []) if s]
        url = str(row.get("applicationLink") or row.get("guid") or "").strip()
        channel, target = application_route(url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max = annualise_salary(
            row.get("minSalary"), row.get("maxSalary"), "year"
        )
        currency = str(row.get("currency") or "").strip().upper() or None
        return {
            "company_name_raw": str(row.get("companyName") or "").strip() or None,
            "title": str(row.get("title") or "").strip(),
            "function_family": categories[0].replace("-", " ") if categories else None,
            "seniority": ", ".join(seniority) or None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            # Only a single named country is a country; a list of eligible
            # countries is a hiring area, not where the job is.
            "country": country_from_location(countries[0]) if len(countries) == 1 else None,
            "work_arrangement": "remote",
            "contract_type": normalise_contract_type(employment),
            "fte_percentage": fte_percentage(employment),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency if (salary_min or salary_max) else None,
            "posted_at": parse_datetime(row.get("pubDate")),
            "language": normalise_language(None, description),
            "application_channel": channel,
            "application_target": target,
            "source_url": url,
        }
