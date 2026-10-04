"""Hacker News "Who is hiring?" adapter (FR-181, FR-183, FR-261, IR-101).

Every month the ``whoishiring`` account opens an "Ask HN: Who is hiring?"
thread, and each top-level comment is one employer's posting, by convention
headed by a single pipe-separated line:

    Acme Robotics | Senior Backend Engineer | Berlin, Germany | ONSITE | €80k-€100k

Endpoints (keyless, the public HN Search API run by Algolia for Hacker News):

    GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring
    -> {"hits": [{"objectID", "title", "created_at"}]}          newest first
    GET https://hn.algolia.com/api/v1/items/{id}
    -> {"id", "title", "children": [{"id", "created_at", "text" (HTML),
                                     "children": [...]}]}       the whole thread

Two requests per plan item, whatever the thread's size: the official Firebase
API (hacker-news.firebaseio.com) would need one request per comment - several
hundred - for the same thread, so the search API is the one that keeps the
traffic proportionate.  Both are public, documented APIs offered for exactly
this kind of reading.

Attribution: ``source_url`` and ``application_target`` are always the
comment's own news.ycombinator.com permalink, so the posting is credited to
its author's comment on Hacker News and read in context there.  The comment's
author name is not stored (CR-402: it identifies a person, not an employer).

Extraction is deterministic: the header line is split on ``|`` and each field
is recognised by shape - a salary by its currency, a role by its job words, a
location by a known country or city - and the comment body becomes the
description.  ``llm_fallback`` is False: free-text comments are exactly where an
extraction model would be tempted to invent a field, and the header carries
what the ``vacancy`` columns need.  A comment without a pipe header is a
reply or a discussion, not a posting, and is skipped; a thread whose comments
have no headers at all raises, so a format change reads as breakage rather
than as an empty month (NFR-403).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

from dreamjob.adapters.base import AdapterCapabilities, PlanItem, RawRecord, register_adapter
from dreamjob.adapters.jobboards._json_feed import JsonFeedAdapter
from dreamjob.adapters.vacancy_source import (
    country_from_location,
    fte_percentage,
    html_to_text,
    keywords_from,
    normalise_contract_type,
    normalise_language,
    normalise_work_arrangement,
    parse_datetime,
    parse_salary_text,
    requested_page,
    split_skills,
)

log = logging.getLogger(__name__)

SEARCH_API = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_API = "https://hn.algolia.com/api/v1/items/{id}"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"
THREAD_PREFIX = "ask hn: who is hiring?"

_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>", re.IGNORECASE)
_URL_RE = re.compile(r"\(?\s*(?:https?://|www\.)\S+\s*\)?", re.IGNORECASE)
_DOMAIN_ONLY_RE = re.compile(r"^[\w.-]+\.[a-z]{2,}(?:/\S*)?$", re.IGNORECASE)
_MONEY_RE = re.compile(r"[$€£]|\b\d{2,3}\s?k\b|\b(?:usd|eur|gbp)\b", re.IGNORECASE)
_ROLE_RE = re.compile(
    r"\b(engineer\w*|developer\w*|programmer\w*|scientist\w*|designer\w*|architect\w*|"
    r"manager\w*|lead|sre|devops|analyst\w*|researcher\w*|cto|head of|intern\w*|"
    r"founding|staff|principal|consultant\w*|specialist\w*|administrator\w*|"
    r"full[- ]?stack|back[- ]?end|front[- ]?end|machine learning|ml|data)\b",
    re.IGNORECASE,
)
_ARRANGEMENT_ONLY_RE = re.compile(
    r"^\s*(remote|onsite|on-site|on site|hybrid|in[- ]office)\b[\s\w(),/+-]*$", re.IGNORECASE
)


def header_fields(header: str) -> dict[str, Any] | None:
    """Recognise the fields of one "Company | Role | Location | ..." line.

    Returns ``None`` when the line has fewer than two fields, which is how a
    reply or a discussion comment is told from a posting.
    """
    segments = [s.strip() for s in header.split("|")]
    segments = [s for s in segments if s]
    if len(segments) < 2:
        return None
    company = _URL_RE.sub("", segments[0]).strip(" -–()")
    role = location = area = None
    salary: tuple[Any, Any, Any] = (None, None, None)
    for segment in segments[1:]:
        bare = _URL_RE.sub("", segment).strip()
        if not bare or _DOMAIN_ONLY_RE.match(bare):
            continue
        if _MONEY_RE.search(bare) and not _ROLE_RE.search(bare):
            if salary == (None, None, None):
                salary = parse_salary_text(bare)
            continue
        if role is None and _ROLE_RE.search(bare):
            role = bare
            continue
        if _ROLE_RE.search(bare):
            continue
        if _ARRANGEMENT_ONLY_RE.match(bare):
            # "REMOTE" alone is an arrangement; "Remote (EU)" also names an
            # area, which is kept as the location when no place is given.
            if area is None and len(bare.split()) > 1:
                area = bare
            continue
        # "San Francisco, CA" names no country this product knows, but a
        # comma-separated place is still the conventional location field.
        if location is None and (country_from_location(bare) or "," in bare):
            location = bare
    location = location or area
    if role is None:
        # No field read as a role: the convention puts it second.
        role = _URL_RE.sub("", segments[1]).strip() or None
    return {
        "company": company or None,
        "role": role,
        "location": location,
        "salary": salary,
    }


@register_adapter
class HnHiringAdapter(JsonFeedAdapter):
    key = "board.hn_hiring"
    display_name = "Hacker News: Who is hiring?"
    coverage_countries: list[str] = []      # worldwide; most postings are US or remote
    rate_limit_rps = 0.5
    legal_notes = (
        "Public HN Search API (hn.algolia.com/api/v1), provided by Algolia for Hacker News, "
        "keyless. Each posting is a user's comment in the monthly 'Ask HN: Who is hiring?' "
        "thread; every record links back to that comment's news.ycombinator.com permalink "
        "as its source and application URL, and the commenter's name is not stored. Two "
        "requests per plan item (find the thread, read it) rather than one per comment."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,
        location_filter=False,
        pagination=False,
        max_results_per_query=1000,
    )

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return self.single_item(
            keywords_from(directives, composite_profile),
            caps,
            "Hacker News's monthly 'Who is hiring?' thread: hundreds of tech employers "
            "posting directly, read in two keyless requests",
            seconds=8,
        )

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = self.native_query(item)
        records: list[RawRecord] = []
        if (requested_page(query) or 1) == 1:
            thread_id = str(query.get("thread_id") or "").strip() or await self._latest_thread()
            if thread_id:
                url = ITEM_API.format(id=thread_id)
                got = await self._get_json(url)
                if got is not None:
                    result, payload = got
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("children"), list
                    ):
                        self.fetch_outcome.ok -= 1
                        self.fetch_outcome.failures.append((url, "not a thread"))
                    else:
                        records.append(
                            self.json_record(
                                url, result, self.feed_meta(query, thread_id=thread_id)
                            )
                        )
        return self.settle(
            records,
            nothing_to_fetch="the thread is read in one response; there is no further page",
        )

    async def _latest_thread(self) -> str | None:
        """The newest "Who is hiring?" story id, or ``None``.

        The ``whoishiring`` account also opens "Who wants to be hired?" and
        "Freelancer? Seeking freelancer?" threads each month, so the title is
        checked, not only the author.  An answered search that names no such
        thread is the source stating it has none.
        """
        url = f"{SEARCH_API}?{urlencode({'tags': 'story,author_whoishiring', 'hitsPerPage': 10})}"
        got = await self._get_json(url)
        if got is None:
            return None
        _, payload = got
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            self.fetch_outcome.ok -= 1
            self.fetch_outcome.failures.append((url, "no hits array"))
            return None
        for hit in hits:
            if isinstance(hit, dict) and str(hit.get("title") or "").lower().startswith(
                THREAD_PREFIX
            ):
                return str(hit.get("objectID") or "").strip() or None
        self.record_stated_empty()
        return None

    def parse(self, raw: RawRecord) -> list[dict]:
        thread = json.loads(raw.content)
        comments = [
            c for c in (thread.get("children") or [])
            if isinstance(c, dict) and c.get("text")
        ]
        out: list[dict] = []
        headed = 0
        for comment in comments:
            text = str(comment["text"])
            header = html_to_text(_PARAGRAPH_RE.split(text, maxsplit=1)[0]).replace("\n", " ")
            fields = header_fields(header)
            if fields is None:
                continue
            headed += 1
            if fields["role"] and self.wanted(header, raw.meta):
                out.append(self._one(comment, header, fields))
        if comments and not headed:
            raise ValueError(
                f"none of {len(comments)} comments carries a 'Company | Role | ...' header; "
                "the thread format has probably changed"
            )
        return out

    def _one(self, comment: dict, header: str, fields: dict[str, Any]) -> dict[str, Any]:
        description = html_to_text(comment.get("text"))
        url = ITEM_URL.format(id=comment.get("id"))
        salary_min, salary_max, currency = fields["salary"]
        required, desirable = split_skills(description)
        location = fields["location"]
        return {
            "company_name_raw": fields["company"],
            "title": str(fields["role"])[:300],
            "description": description,
            "required_skills": required,
            "desirable_skills": desirable,
            "location": location,
            "country": country_from_location(location) if location else None,
            "work_arrangement": normalise_work_arrangement(header),
            "contract_type": normalise_contract_type(header),
            "fte_percentage": fte_percentage(header),
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": currency,
            "posted_at": parse_datetime(comment.get("created_at") or comment.get("created_at_i")),
            "language": normalise_language(None, description),
            # The comment is the posting: attribution and the place to read it
            # in full, with any application instructions its author gave.
            "application_channel": "url",
            "application_target": url,
            "source_url": url,
        }
