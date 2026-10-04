"""Adzuna adapter - the Adzuna job-search API, free with a developer key (FR-181, IR-101).

Endpoint (documented at developer.adzuna.com, free registration for an
``app_id``/``app_key`` pair):

    GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
        ?app_id=...&app_key=...&results_per_page=50&what=...&category=it-jobs
    -> {"count": N, "results": [{"id", "title", "description" (snippet),
        "created" (ISO), "redirect_url", "company": {"display_name"},
        "location": {"display_name", "area": [...]}, "category": {"tag", "label"},
        "contract_type", "contract_time", "salary_min", "salary_max",
        "salary_is_predicted" ("0"/"1"), "latitude", "longitude"}]}

Adzuna aggregates the national boards of the countries it serves - Belgium,
the Netherlands, France, Germany and the United Kingdom among them - which is
what makes it the one keyed source here that covers this product's own
markets rather than remote work.

The terms of the API: data shown to a user must be attributed to Adzuna
("Jobs by Adzuna", linked), clicks must go through the ``redirect_url`` Adzuna
returns, and the free tier is rate limited (of the order of 25 calls a minute
and 250 a day).  IR-101 makes that binding:

* **Attribution.**  ``source_url`` and ``application_target`` are always the
  posting's ``redirect_url``, never a URL taken out of the snippet.
* **Rate.**  At most ``MAX_KEYWORDS`` searches per country per page, and one
  plan item per country; the catalogue rate is 0.4 req/s.
* **The terms travel with the data** in ``legal_notes``.

No key, no traffic: without ``DREAMJOB_ADZUNA_APP_ID`` and
``DREAMJOB_ADZUNA_APP_KEY`` the adapter implements the ``available()`` /
``unavailable_reason()`` contract the registry adapters use, so the planner
leaves it out with that reason and it never issues a request (FR-245).  The
pair is sent as query parameters through the egress layer, so it never appears
in the URL this adapter records on a raw document.

A salary Adzuna *predicted* (``salary_is_predicted == "1"``) is not stored:
FR-261 records a posted range "if stated", and an estimate is not one.  The
description is the API's snippet, not the full advert.  ``llm_fallback`` is
False: 0 tokens per collection.
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
    countries_from,
    country_terms,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    query_terms,
    requested_page,
    split_skills,
)
from dreamjob.config import get_settings

log = logging.getLogger(__name__)

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
PAGE_SIZE = 50
DEFAULT_CATEGORY = "it-jobs"
#: Searches per country per page.  The free tier is counted in calls a day, so
#: the keyword list is cut here rather than spent one search per synonym.
MAX_KEYWORDS = 3
#: Ceiling on one plan item's walk, whatever the caps say.
MAX_PAGES_PER_ITEM = 5

#: The countries the API serves (ISO-2, upper case) and the currency their
#: salaries are stated in.  Adzuna's own path segment is the lower-cased code.
CURRENCY_BY_COUNTRY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF",
    "DE": "EUR", "ES": "EUR", "FR": "EUR", "GB": "GBP", "IN": "INR", "IT": "EUR",
    "MX": "MXN", "NL": "EUR", "NZ": "NZD", "PL": "PLN", "SG": "SGD", "US": "USD",
    "ZA": "ZAR",
}


@register_adapter
class AdzunaAdapter(JsonFeedAdapter):
    key = "board.adzuna"
    display_name = "Adzuna"
    coverage_countries: list[str] = sorted(CURRENCY_BY_COUNTRY)
    rate_limit_rps = 0.4
    legal_notes = (
        "Adzuna job-search API (developer.adzuna.com), free with a registered app_id/app_key. "
        "Terms: results must be attributed to Adzuna ('Jobs by Adzuna', linked) and clicks "
        "must go through the redirect_url Adzuna returns; the free tier is rate limited "
        "(about 25 calls a minute, 250 a day). Honoured by keeping redirect_url as the "
        "source and application URL of every record and by at most "
        f"{MAX_KEYWORDS} searches per country per page. Inert until "
        "DREAMJOB_ADZUNA_APP_ID and DREAMJOB_ADZUNA_APP_KEY are set."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True,
        location_filter=True,
        pagination=True,
        max_results_per_query=PAGE_SIZE,
    )

    # -- availability (FR-245) ----------------------------------------------
    @staticmethod
    def _credentials() -> tuple[str, str]:
        settings = get_settings()
        return settings.adzuna_app_id.strip(), settings.adzuna_app_key.strip()

    def available(self) -> bool:
        """False without both halves of the key; the planner then leaves it out."""
        app_id, app_key = self._credentials()
        return bool(app_id and app_key)

    def unavailable_reason(self) -> str:
        return (
            "Adzuna needs a free developer key; set DREAMJOB_ADZUNA_APP_ID and "
            "DREAMJOB_ADZUNA_APP_KEY in the environment (developer.adzuna.com)"
        )

    # -- plan (FR-162, FR-164) ----------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        if not self.available():
            return []
        keywords = keywords_from(directives, composite_profile)[:MAX_KEYWORDS]
        pages = max(1, min(MAX_PAGES_PER_ITEM, int(caps.get("max_pages_per_source") or 2)))
        searches = max(1, len(keywords))
        items: list[PlanItem] = []
        for country in countries_from(directives):
            if country not in CURRENCY_BY_COUNTRY:
                continue
            items.append(
                PlanItem(
                    adapter_key=self.key,
                    native_query={
                        "country": country,
                        "keywords": keywords,
                        "category": DEFAULT_CATEGORY,
                        "pages": 1,
                        "title_filter": bool(caps.get("title_filter")),
                    },
                    rationale=(
                        f"Adzuna aggregates the national boards of {country}; "
                        f"{searches} search(es) x {pages} page(s) of {PAGE_SIZE}"
                    ),
                    estimated_pages=pages,
                    estimated_seconds=3 * searches * pages,
                    caps={"max_records": PAGE_SIZE * searches * pages},
                )
            )
        return items

    # -- fetch (IR-102, FR-182, FR-185) -------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        records: list[RawRecord] = []
        app_id, app_key = self._credentials()
        countries = [c for c in country_terms(query) if c in CURRENCY_BY_COUNTRY]
        page = requested_page(query) or 1
        if not (app_id and app_key) or not countries or page > MAX_PAGES_PER_ITEM:
            return self.settle(records, nothing_to_fetch=self._nothing_to_fetch(
                bool(app_id and app_key), countries, page
            ))
        country = countries[0]
        keywords = query_terms(query)[:MAX_KEYWORDS] or [""]
        category = str(query.get("category") or DEFAULT_CATEGORY)
        url = API.format(country=country.lower(), page=page)
        for keyword in keywords:
            params: dict[str, Any] = {
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": PAGE_SIZE,
                "content-type": "application/json",
            }
            if category:
                params["category"] = category
            if keyword:
                params["what"] = keyword
            got = await self._get_json(url, params=params)
            if got is None:
                continue
            result, payload = got
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                self.fetch_outcome.ok -= 1
                self.fetch_outcome.failures.append((url, "no results array"))
                continue
            # The recorded URL names the search without the key (NFR-203).
            label = f"{url}?{urlencode({'what': keyword})}" if keyword else url
            records.append(
                self.json_record(label, result, self.feed_meta(query, country=country))
            )
        return self.settle(records)

    @staticmethod
    def _nothing_to_fetch(keyed: bool, countries: list[str], page: int) -> str:
        if not keyed:
            return "Adzuna is not configured: DREAMJOB_ADZUNA_APP_ID/APP_KEY are not set"
        if not countries:
            return "the plan item names no country Adzuna serves"
        return f"page {page} is beyond the {MAX_PAGES_PER_ITEM}-page bound"

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        rows = json.loads(raw.content).get("results") or []
        country = str(raw.meta.get("country") or "").upper() or None
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = html_to_text(row.get("title")).strip()
            if title and self.wanted(title, raw.meta):
                out.append(self._one(row, title, country))
        return out

    def _one(self, row: dict, title: str, country: str | None) -> dict[str, Any]:
        description = html_to_text(row.get("description"))
        location = row.get("location") if isinstance(row.get("location"), dict) else {}
        company = row.get("company") if isinstance(row.get("company"), dict) else {}
        category = row.get("category") if isinstance(row.get("category"), dict) else {}
        contract_type = str(row.get("contract_type") or "")
        contract_time = str(row.get("contract_time") or "")
        url = str(row.get("redirect_url") or "").strip()
        channel, target = application_route(url, None)
        required, desirable = split_skills(description)
        # A predicted salary is Adzuna's estimate, not the advert's statement.
        predicted = str(row.get("salary_is_predicted") or "0") == "1"
        salary_min, salary_max = (
            (None, None) if predicted
            else annualise_salary(row.get("salary_min"), row.get("salary_max"), "year")
        )
        return {
            "company_name_raw": str(company.get("display_name") or "").strip() or None,
            "title": title,
            "function_family": str(category.get("label") or "").strip() or None,
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": str(location.get("display_name") or "").strip() or None,
            "country": country,
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "work_arrangement": normalise_work_arrangement(title, description),
            # ``contract_type`` is what the posting states ("permanent" or
            # "contract"); ``contract_time`` is consulted only when it states
            # nothing, because "full_time" would otherwise read a contract
            # role as permanent.
            "contract_type": normalise_contract_type(contract_type)
            if contract_type else normalise_contract_type(contract_time),
            "fte_percentage": fte_percentage(contract_time),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": (
                CURRENCY_BY_COUNTRY.get(country or "") if (salary_min or salary_max) else None
            ),
            "posted_at": parse_datetime(row.get("created")),
            "language": normalise_language(None, description),
            "application_channel": channel,
            "application_target": target,
            "source_url": url,
        }
