"""Remote OK adapter - the public Remote OK JSON feed (FR-181, FR-261, IR-101).

Endpoint (keyless, linked from remoteok.com as its public API):

    GET https://remoteok.com/api
    -> [{"last_updated": ..., "legal": "API Terms of Service: ..."},
        {"id", "slug", "epoch", "date" (ISO), "company", "position", "tags": [...],
         "description" (HTML), "location", "salary_min", "salary_max" (USD/year, 0 = none),
         "apply_url", "url"}, ...]

The first array element is not a job: it is the service's legal notice, and it
states the terms - link back to the job's URL on Remote OK (a followed link)
and mention Remote OK as the source, or API access is suspended; do not use
the Remote OK logo without permission.  IR-101 makes that binding:

* **Link back.**  ``source_url`` is always the posting's remoteok.com ``url``,
  so every stored row and everything shown to a user names Remote OK and leads
  back to it.  The employer's ``apply_url`` is kept only as the application
  route, never in place of the source.
* **The legal element is read, not stored.**  It is skipped by shape (no
  ``position``), and its text travels in ``legal_notes`` for the FR-185 source
  dashboard.
* **One request per plan item.**  The feed is a single answer; the documented
  surface has no search parameter, so keyword filtering happens on the rows
  that came back.  Logos are never fetched.

Remote-only, global coverage.  ``salary_min``/``salary_max`` are the posted
annual range in US dollars; ``0`` means "not stated" and stays empty (FR-261:
"if stated").  ``llm_fallback`` is False: 0 tokens per collection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dreamjob.adapters.base import AdapterCapabilities, PlanItem, RawRecord, register_adapter
from dreamjob.adapters.jobboards._json_feed import JsonFeedAdapter
from dreamjob.adapters.vacancy_source import (
    annualise_salary,
    application_route,
    country_from_location,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    parse_datetime,
    requested_page,
    split_skills,
)

log = logging.getLogger(__name__)

API = "https://remoteok.com/api"


@register_adapter
class RemoteOkAdapter(JsonFeedAdapter):
    key = "board.remoteok"
    display_name = "Remote OK"
    coverage_countries: list[str] = []      # remote roles, worldwide
    rate_limit_rps = 0.2
    legal_notes = (
        "Free public JSON feed (remoteok.com/api). Its first element is the legal notice: "
        "'Please link back (with follow, no nofollow!) to the URL on Remote OK and mention "
        "Remote OK as a source ... If you do not we'll have to suspend API access.' The "
        "Remote OK logo may not be used without written permission. Honoured by keeping "
        "the remoteok.com posting as the source URL of every record, by one request per "
        "plan item and by never fetching logos."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,
        location_filter=False,
        work_arrangement_filter=True,
        pagination=False,
        max_results_per_query=500,
    )

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return self.single_item(
            keywords_from(directives, composite_profile),
            caps,
            "Remote OK publishes its current remote jobs as one keyless JSON feed; "
            "one request, filtered on our side",
        )

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        records: list[RawRecord] = []
        if (requested_page(query) or 1) == 1:
            got = await self._get_json(API)
            if got is not None:
                result, payload = got
                if not isinstance(payload, list):
                    self.fetch_outcome.ok -= 1
                    self.fetch_outcome.failures.append((API, "not a job array"))
                else:
                    records.append(self.json_record(API, result, self.feed_meta(query)))
        return self.settle(
            records,
            nothing_to_fetch="Remote OK answers in one response; there is no further page",
        )

    def parse(self, raw: RawRecord) -> list[dict]:
        rows = json.loads(raw.content)
        out: list[dict] = []
        for row in rows:
            # The legal notice (and anything else that is not a posting) has no
            # ``position``; it is skipped by shape rather than by index, so a
            # feed that one day drops or moves it still parses.
            if not isinstance(row, dict):
                continue
            title = str(row.get("position") or "").strip()
            if title and self.wanted(title, raw.meta):
                out.append(self._one(row))
        return out

    def _one(self, row: dict) -> dict[str, Any]:
        description = html_to_text(row.get("description"))
        location = str(row.get("location") or "").strip()
        tags = [str(t) for t in (row.get("tags") or []) if t]
        url = str(row.get("url") or "").strip()
        if not url and row.get("slug"):
            url = f"https://remoteok.com/remote-jobs/{row['slug']}"
        # The employer's apply link is the route to apply; the remoteok.com page
        # stays the source, which is the attribution the terms require.
        channel, target = application_route(
            str(row.get("apply_url") or "").strip() or url, description
        )
        required, desirable = split_skills(description)
        salary_min, salary_max = annualise_salary(
            row.get("salary_min"), row.get("salary_max"), "year"
        )
        return {
            "company_name_raw": str(row.get("company") or "").strip() or None,
            "title": str(row.get("position") or "").strip(),
            "function_family": tags[0] if tags else None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(location),
            "work_arrangement": "remote",
            "contract_type": normalise_contract_type(*tags),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": "USD" if (salary_min or salary_max) else None,
            "posted_at": parse_datetime(row.get("date") or row.get("epoch")),
            "language": normalise_language(None, description),
            "application_channel": channel,
            "application_target": target,
            "source_url": url,
        }
