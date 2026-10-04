"""Shared base for the public JSON job feeds (FR-181, FR-183, IR-101, NFR-403).

Remotive, RemoteOK, Jobicy, Himalayas, Adzuna and the Hacker News hiring
thread all answer one structured JSON document per request, and all six do the
same three things with it: refuse a body that is not JSON (a bot wall, an HTML
error page), keep the parsed rows with the keywords the plan item carried, and
filter on the title only when the plan asked for it.  That part lives here once;
each adapter keeps its own endpoint, terms and field mapping.

The leading underscore keeps :func:`dreamjob.adapters.discover` from importing
this module as an adapter: it registers nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from dreamjob.adapters.base import AccessMethod, PlanItem, RawRecord, SourceType, ToSStatus
from dreamjob.adapters.vacancy_source import VacancySourceAdapter, query_terms, title_matches
from dreamjob.egress.client import FetchResult

log = logging.getLogger(__name__)


class JsonFeedAdapter(VacancySourceAdapter):
    """A job board reached through a documented JSON API.

    ``empty_parse_is_stated`` holds because the payload is machine-readable: a
    well-formed answer with no rows is the source saying it holds nothing, not
    a failed extraction.  A body that is not JSON never reaches ``parse`` - it
    is recorded as a failed request, so a layout change or a block still
    surfaces as breakage (NFR-403, FR-185).
    """

    source_type = SourceType.JOB_BOARD
    access_method = AccessMethod.API
    tos_status = ToSStatus.PERMITTED
    llm_fallback = False
    base_confidence = 0.8
    empty_parse_is_stated = True

    async def _get_json(self, url: str, **kwargs: Any) -> tuple[FetchResult, Any] | None:
        """One GET through the egress layer, decoded; ``None`` when unusable.

        The two refusals below undo the ``ok`` that :meth:`_get` already
        counted: an answered request whose body cannot be read is a failure,
        and :meth:`settle` must see it as one.
        """
        headers = {"Accept": "application/json", **(kwargs.pop("headers", None) or {})}
        result = await self._get(url, headers=headers, **kwargs)
        if result is None:
            return None
        if result.text.lstrip()[:1] not in ("{", "["):
            log.warning("[%s] %s did not answer JSON", self.key, url)
            self.fetch_outcome.ok -= 1
            self.fetch_outcome.failures.append((url, "not JSON"))
            return None
        try:
            payload = json.loads(result.text)
        except ValueError:
            self.fetch_outcome.ok -= 1
            self.fetch_outcome.failures.append((url, "malformed JSON"))
            return None
        return result, payload

    @staticmethod
    def feed_meta(query: dict[str, Any], **extra: Any) -> dict[str, Any]:
        """What ``parse`` needs from the plan item, carried on the raw record.

        Keywords are read in either vocabulary (the adapter's own ``keywords``
        or the campaign planner's ``query``), see :func:`query_terms`.
        """
        return {
            "keywords": query_terms(query),
            "title_filter": bool(query.get("title_filter")),
            **extra,
        }

    @staticmethod
    def wanted(text: str, meta: dict[str, Any]) -> bool:
        """The client-side title filter, applied only when the plan item asks.

        Off by default, as on Arbeitnow and the ATS boards: relevance is the
        ranking stage's job, and a filter here would drop a posting before it
        could be scored (FR-162).
        """
        return not meta.get("title_filter") or title_matches(text, meta.get("keywords") or [])

    def json_record(self, url: str, result: FetchResult, meta: dict[str, Any]) -> RawRecord:
        return RawRecord(
            url=url,
            content=result.text,
            content_type="application/json",
            raw_document_id=result.raw_document_id,
            meta=meta,
        )

    def single_item(
        self, keywords: list[str], caps: dict, rationale: str, *, seconds: int = 4,
        **native: Any,
    ) -> list[PlanItem]:
        """The one plan item a single-response feed needs (FR-162, FR-186)."""
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={
                    "keywords": keywords,
                    "title_filter": bool(caps.get("title_filter")),
                    **native,
                },
                rationale=rationale,
                estimated_pages=1,
                estimated_seconds=seconds,
            )
        ]
