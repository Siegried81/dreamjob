"""Jobicy adapter - the public Jobicy remote-jobs API (FR-181, FR-261, IR-101).

Endpoint (keyless, documented on jobicy.com with its RSS feed):

    GET https://jobicy.com/api/v2/remote-jobs?count=50[&geo=...][&industry=...][&tag=...]
    -> {"apiVersion": "2", "friendlyNotice": "...", "jobCount": N,
        "jobs": [{"id", "url", "jobSlug", "jobTitle", "companyName",
                  "jobIndustry": [...], "jobType": [...], "jobGeo", "jobLevel",
                  "jobExcerpt", "jobDescription" (HTML), "pubDate",
                  "annualSalaryMin", "annualSalaryMax", "salaryCurrency"}]}

Jobicy's terms for the feed: link back to the job's page on Jobicy and credit
Jobicy as the source; do not redistribute the listings to other job boards
(Jooble, Google Jobs, LinkedIn and the like); check for new jobs only a few
times a day.  IR-101 makes that binding:

* **Link back.**  ``source_url`` and ``application_target`` are always the
  posting's jobicy.com URL.
* **A few times a day.**  One request per plan item, cached for six hours.
  Keyword filtering happens on the rows that came back rather than as one
  ``tag`` request per keyword.  ``geo`` and ``industry`` are sent only when
  the plan item names them, because their accepted values are the service's
  vocabulary and a guessed one returns an error instead of jobs.
* **The terms travel with the data** in ``legal_notes``.

Remote-only.  ``jobGeo`` says where a candidate may live ("Europe", "USA",
"Anywhere"), so coverage is global.  ``annualSalaryMin``/``Max`` are the posted
annual range.  ``llm_fallback`` is False: 0 tokens per collection.
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
    unescape_entities,
)

log = logging.getLogger(__name__)

API = "https://jobicy.com/api/v2/remote-jobs"
#: The page size requested.  The service's documented ceiling is higher; fifty
#: keeps one answer small while covering a day of new remote postings.
COUNT = 50
CACHE_SECONDS = 6 * 3600


@register_adapter
class JobicyAdapter(JsonFeedAdapter):
    key = "board.jobicy"
    display_name = "Jobicy"
    coverage_countries: list[str] = []      # remote roles, worldwide
    rate_limit_rps = 0.05
    legal_notes = (
        "Free public API (jobicy.com/api/v2/remote-jobs). Terms: link back to the job's "
        "page on Jobicy and credit Jobicy as the source; do not redistribute the listings "
        "to third-party job boards; check for new jobs only a few times a day. Honoured "
        "by keeping the jobicy.com posting as the source and application URL of every "
        "record, by one request per plan item and by a six-hour cache on it."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,
        location_filter=False,
        work_arrangement_filter=True,
        pagination=False,
        max_results_per_query=COUNT,
    )

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return self.single_item(
            keywords_from(directives, composite_profile),
            caps,
            "Jobicy publishes its latest remote jobs as one keyless JSON answer; one "
            "request, cached six hours as its terms ask",
            count=COUNT,
        )

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        records: list[RawRecord] = []
        if (requested_page(query) or 1) == 1:
            params: dict[str, Any] = {"count": int(query.get("count") or COUNT)}
            for name in ("geo", "industry"):
                if query.get(name):
                    params[name] = str(query[name])
            url = f"{API}?{urlencode(params)}"
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
            nothing_to_fetch="Jobicy answers in one response; there is no further page",
        )

    def parse(self, raw: RawRecord) -> list[dict]:
        rows = json.loads(raw.content).get("jobs") or []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = unescape_entities(str(row.get("jobTitle") or "")).strip()
            if title and self.wanted(title, raw.meta):
                out.append(self._one(row, title))
        return out

    def _one(self, row: dict, title: str) -> dict[str, Any]:
        description = html_to_text(row.get("jobDescription") or row.get("jobExcerpt"))
        location = str(row.get("jobGeo") or "").strip()
        job_types = [str(t) for t in _as_list(row.get("jobType")) if t]
        industries = [unescape_entities(str(t)) for t in _as_list(row.get("jobIndustry")) if t]
        url = str(row.get("url") or "").strip()
        channel, target = application_route(url, description)
        required, desirable = split_skills(description)
        salary_min, salary_max = annualise_salary(
            row.get("annualSalaryMin"), row.get("annualSalaryMax"), "year"
        )
        currency = str(row.get("salaryCurrency") or "").strip().upper() or None
        level = str(row.get("jobLevel") or "").strip()
        return {
            "company_name_raw": unescape_entities(str(row.get("companyName") or "")).strip()
            or None,
            "title": title,
            "function_family": industries[0] if industries else None,
            "seniority": level if level and level.lower() != "any" else None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location or None,
            "country": country_from_location(location),
            "work_arrangement": "remote",
            "contract_type": normalise_contract_type(*job_types),
            "fte_percentage": fte_percentage(*job_types),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency if (salary_min or salary_max) else None,
            "posted_at": parse_datetime(row.get("pubDate")),
            "language": normalise_language(None, description),
            "application_channel": channel,
            "application_target": target,
            "source_url": url,
        }


def _as_list(value: Any) -> list[Any]:
    """``jobType``/``jobIndustry`` are arrays, but a single string is accepted too."""
    if isinstance(value, list):
        return value
    return [value] if value else []
