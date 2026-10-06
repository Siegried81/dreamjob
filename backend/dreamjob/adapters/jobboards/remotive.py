"""Remotive adapter - a free public API for remote tech jobs (FR-181, FR-261, IR-101).

Endpoint (keyless, documented at remotive.com/api-documentation and in the
remotive-com/remote-jobs-api repository):

    GET https://remotive.com/api/remote-jobs?category=software-dev
    -> {"0-legal-notice": "...", "job-count": N,
        "jobs": [{"id", "url", "title", "company_name", "category", "tags": [...],
                  "job_type" ("full_time", "contract", ...), "publication_date" (ISO),
                  "candidate_required_location", "salary" (free text),
                  "description" (HTML)}]}

The terms, stated in the documentation and in ``0-legal-notice``: link back to
the posting's URL on Remotive and mention Remotive as the source; do not
re-publish the jobs on third-party boards; the data is delayed by 24 hours and
the API should be fetched at most about four times a day.  IR-101 makes that
binding, and this adapter keeps it in three ways:

* **Link back.**  ``source_url`` and ``application_target`` are always the
  posting's remotive.com URL, so every stored row and everything shown to a
  user carries the attribution.
* **Four times a day.**  One request per plan item - the whole ``software-dev``
  category, which the service returns in a single answer - and that request is
  cached for six hours (``max_age``), above the hourly vacancy TTL the other
  boards use.  Keyword filtering is done on the rows that came back, never by
  issuing one ``search`` request per keyword.
* **The terms travel with the data** in ``legal_notes``, which the FR-185
  source dashboard shows the administrator.

Remote-only by construction: ``work_arrangement`` is always ``remote`` and
coverage is global (``candidate_required_location`` says where a candidate may
live, e.g. "Europe", "USA", "Worldwide").  ``llm_fallback`` is False: the
payload is structured, so collection costs 0 tokens.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from dreamjob.adapters.base import (
    AdapterCapabilities,
    PlanItem,
    RawRecord,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.jobboards._json_feed import JsonFeedAdapter
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
)

log = logging.getLogger(__name__)

API = "https://remotive.com/api/remote-jobs"
DEFAULT_CATEGORY = "software-dev"
#: Six hours: at most four answers a day per category, as the terms ask.
CACHE_SECONDS = 6 * 3600


@register_adapter
class RemotiveAdapter(JsonFeedAdapter):
    key = "board.remotive"
    display_name = "Remotive"
    coverage_countries: list[str] = []      # remote roles; candidates may live anywhere allowed
    rate_limit_rps = 0.05
    # docs/Data_Gathering_Plan.md rule 3 names "Remotive's /api/*" among the
    # paths not to fetch through the egress layer, as robots-disallowed.  The
    # JsonFeedAdapter default of PERMITTED therefore states the opposite of the
    # project's own finding: it registers the adapter enabled, tells the source
    # dashboard it is permitted, and has the planner budget items for it, while
    # the robots gate refuses the fetch at runtime.  Declared the way
    # board.jobat was settled - the operator has to acknowledge it.
    tos_status = ToSStatus.RESTRICTED
    requires_ack = True
    legal_notes = (
        "Free public API (remotive.com/api/remote-jobs). Terms: link back to the job's URL "
        "on Remotive and mention Remotive as the source; do not submit Remotive jobs to "
        "third-party job boards; data is delayed 24 h and should be fetched at most about "
        "four times a day. Honoured by keeping the remotive.com posting as the source and "
        "application URL of every record, by one request per plan item and by a six-hour "
        "cache on that request."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,       # one category request; filtering is client-side
        location_filter=False,
        work_arrangement_filter=True,
        pagination=False,
        max_results_per_query=1000,
    )

    # -- plan (FR-162) ------------------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return self.single_item(
            keywords_from(directives, composite_profile),
            caps,
            "Remotive publishes its remote software-development jobs as one keyless JSON "
            "answer; one request, cached six hours as its terms ask",
            category=DEFAULT_CATEGORY,
        )

    # -- fetch (IR-102, FR-182, FR-185) -------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        records: list[RawRecord] = []
        if (requested_page(query) or 1) == 1:
            category = str(query.get("category") or DEFAULT_CATEGORY)
            url = f"{API}?{urlencode({'category': category})}"
            got = await self._get_json(url, max_age=CACHE_SECONDS)
            if got is not None:
                result, payload = got
                if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                    self.fetch_outcome.ok -= 1
                    self.fetch_outcome.failures.append((url, "no jobs array"))
                else:
                    records.append(self.json_record(url, result, self.feed_meta(query)))
        return self.settle(
            records,
            nothing_to_fetch="Remotive answers in one response; there is no further page",
        )

    # -- parse (FR-183) -----------------------------------------------------
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
        description = html_to_text(row.get("description"))
        location = str(row.get("candidate_required_location") or "").strip()
        job_type = str(row.get("job_type") or "")
        # IR-101 "link back": the remotive.com page is the source and the route
        # to apply, so the attribution survives into every stored row.
        url = str(row.get("url") or "").strip()
        channel, target = application_route(url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max, currency = parse_salary_text(str(row.get("salary") or ""))
        return {
            "company_name_raw": str(row.get("company_name") or "").strip() or None,
            "title": str(row.get("title") or "").strip(),
            "function_family": str(row.get("category") or "").strip() or None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(location),
            "work_arrangement": "remote",
            "contract_type": normalise_contract_type(job_type),
            "fte_percentage": fte_percentage(job_type),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(row.get("publication_date")),
            "language": normalise_language(None, description),
            "application_channel": channel,
            "application_target": target,
            "source_url": url,
        }
