"""Jobat.be adapter - Belgian commercial job board (FR-181, IR-101, FR-183).

Jobat publishes structured listing pages and emits schema.org ``JobPosting``
on its advert pages, so extraction is deterministic where the site answers.

It also sits behind a Cloudflare filter that answers HTTP 403 to any client it
does not recognise - including ``robots.txt``-advertised paths such as
``/sitemaps/sitemap.xml`` and the search pages this adapter reads.  That 403 is
now what it is: every request of the plan item failed, so the plan item fails
with "HTTP 403" against its name (FR-185).  It used to be swallowed and
recorded as ``done`` with zero records, which read on the dashboard exactly
like a board with no matching vacancies.  Whether the block is permanent is a
property of the network the collector runs on, so the adapter stays enabled and
RESTRICTED and lets the operator see the failures and decide.
"""

from __future__ import annotations

from typing import Any

from dreamjob.adapters.base import AdapterCapabilities, ToSStatus, register_adapter
from dreamjob.adapters.jobboards.generic_html import HtmlBoardAdapter


@register_adapter
class JobatAdapter(HtmlBoardAdapter):
    key = "board.jobat"
    display_name = "Jobat.be"
    coverage_countries = ["BE"]
    tos_status = ToSStatus.RESTRICTED
    # IR-101 acknowledgement is now REQUIRED. It was False, and on 2026-10-04
    # this adapter fetched www.jobat.be 23 times with HTTP 200 on every one
    # (http_cache, 14:14:04 -> 15:03:01) - while docs/Data_Gathering_Plan.md
    # says "Do not touch Indeed, LinkedIn, StepStone or Jobat". The docstring
    # below reasons that the 403 keeps collection harmless; the 23 successes
    # say the 403 is not reliable, so "enabled" was not harmless either.
    # `is_enabled()` (adapters/base.py:184) now refuses until an administrator
    # acknowledges, which puts the decision in front of a human instead of
    # leaving it to whether Cloudflare happens to answer.
    requires_ack = True
    rate_limit_rps = 0.3
    legal_notes = (
        "Commercial board; terms restrict systematic re-use. Collection is limited to "
        "public advert pages at a low rate and stores only what the advert states."
    )
    capabilities = AdapterCapabilities(
        keyword_search=True, location_filter=True, contract_type_filter=True,
        pagination=True, max_results_per_query=100,
    )

    defaults: dict[str, Any] = {
        "url_template": (
            "https://www.jobat.be/nl/jobs?trefwoord={query}&plaats={location}&pagina={page}"
        ),
        "pages": 2,
        "country": "BE",
        "language": "nl",
        "detail": True,
        "selectors": {
            "list_item": "article.job, .job-card, li[data-jobid]",
            "url": 'a[href*="/nl/jobs/"]@href || a@href',
            "title": "h2 || h3 || .job-title",
            "company": ".company, .job-company",
            "location": ".location, .job-location",
            "posted_at": "time@datetime || time",
        },
        "detail_selectors": {
            "title": "h1",
            "company": '[itemprop="hiringOrganization"] || .company-name',
            "location": '[itemprop="jobLocation"] || .location',
            "description": '[itemprop="description"] || .job-description || main',
            "posted_at": "time@datetime",
            "apply_url": 'a[href^="mailto:"]@href || a.apply@href',
        },
    }
