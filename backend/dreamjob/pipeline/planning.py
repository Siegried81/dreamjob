"""Campaign planning: which sources, and what to ask each of them (FR-161..166).

Planning is the step between "here is my dream job" and "go and collect".  It
runs in five passes:

0. **Discover** (FR-162) - materialise the *targets* the selected sources will
   be pointed at: one board per (vendor, slug) from the registry, the knowledge
   base and the companies the seeker named, and one EURES item per region x
   sector x period.  Without this pass every company-scoped source was planned
   once with an empty target list and then reported "done, 0 records", which is
   why the first gathering phase retrieved nothing at all.  Discovery is
   deterministic and spends no tokens (see :mod:`dreamjob.pipeline.discovery`).
1. **Select** (FR-161, FR-164) - read the source catalogue and keep only the
   sources whose coverage, terms of service and access method fit this search.
   A Benelux search never plans an Asian job board, and every rejection is kept
   with its reason so the plan screen can explain itself.
2. **Translate** (FR-162) - the LLM turns the composite profile, the directives
   and the dream job model into each surviving source's *native* query form:
   keyword strings and filter parameters for a job board, a board slug for an
   ATS, a sector code for a registry, crawl seeds for a website crawler, search
   URLs for LinkedIn.  Sources are translated in small batches, so a failed
   call costs only its own sources.  When the model is unavailable or the token
   budget is nearly spent, a deterministic query is built from the directives
   instead (NFR-104).
3. **Estimate and persist** (FR-163, FR-166) - pages, duration and cost per
   source, written as ``source_plan_item`` rows that the user reviews, edits or
   excludes before anything is fetched, and that every collected record is
   later linked back to.
4. **Reuse** (FR-342) - the knowledge base is queried and plan items that are
   already covered by fresh data are skipped, with the saving reported.

The LinkedIn network strategy (FR-165) is planned as its own item: first-degree
contacts, people in similar roles and alumni of the same employers and schools,
under an explicit maximum number of profiles and companies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.base import PlanItem as AdapterPlanItem
from dreamjob.adapters.base import SourceAdapter, adapter_unavailable_reason, get_adapter
from dreamjob.adapters.vacancy_source import (
    country_terms,
    location_terms,
    query_terms,
    requested_page,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import campaigns as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError, redact
from dreamjob.pipeline import declines, discovery, knowledge_base, top_employers
from dreamjob.pipeline.declines import NoUsableSources

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "campaign_plan.md"

# Per-source ceilings applied before the user ever sees the plan (FR-186).
#
# A "page" means one whole board for ATS adapters (pagination=False in the
# catalogue), 50 rows for board.eures, 50 offers for board.actiris and
# 250 rows for board.arbeitnow. max_pages_per_source applies per plan item;
# ATS items are one page each, so it bounds only the paginated boards.
#
# Sizing (Campaign A, docs/Data_Gathering_Plan.md section 2.4): ~6,900 ATS
# boards + 600 EURES pages + ~20 Actiris pages ~ 7,500 pages; 10,000 leaves
# room for a full-registry refresh tranche. max_companies is the 7,500 target
# plus slack and is only meaningful once collection writes company rows.
DEFAULT_CAPS: dict[str, int] = {
    "max_pages": 10_000,            # was 200
    "max_pages_per_source": 20,     # unchanged; partition EURES by region x sector x period
    "max_companies": 8_000,         # was 200
    "max_people": 100,              # unchanged; not exercised in the first phase
    "max_duration_seconds": 4 * 3600,  # unchanged (NFR-103)
    # A ceiling on the ranked list, not a target.  One campaign synthesised
    # 48,269 opportunity rows and every later pass walked all of them; keeping
    # the cap in the schema makes it visible and adjustable rather than implicit
    # in whatever the sources returned.
    "max_opportunities": 25_000,
}

#: The bounds an *unattended* run puts on the campaign it creates (FR-186).
#:
#: These are deliberately tighter than :data:`DEFAULT_CAPS`.  Those are sized
#: for an operator's supervised data-gathering run; autopilot is the opposite -
#: a job seeker pressed one button and is waiting for a shortlist.  They live
#: here, next to the defaults they tighten, so the autopilot page cap and the
#: campaign defaults are one decision in one place: ``autopilot`` imports this
#: mapping and no longer keeps a second copy that can drift.
#:
#: ``max_pages`` is 400: 150 scaled a real plan down from 1,505 pages and left
#: most of the plan unfetched, while 10,000 is a supervised sweep's budget.  400
#: costs roughly 25 minutes of API reads at the catalogue's rates and keeps the
#: plan's cost estimate on the start screen, which is what "visible" means
#: (FR-163, FR-186).
DEFAULT_AUTOPILOT_CAPS: dict[str, int] = {
    "max_companies": 40,
    "max_pages": 400,
    "max_pages_per_source": 8,
    "max_duration_seconds": 45 * 60,
    # An unattended run ranks what one press of a button asked for; a corpus of
    # tens of thousands is neither reviewable nor the point of a shortlist.
    "max_opportunities": 5_000,
}

# FR-165: the network crawl is slow and intrusive, so it is capped hard.
LINKEDIN_NETWORK_ADAPTER = "linkedin_network"
DEFAULT_NETWORK_CAPS = {"max_profiles": 50, "max_companies": 40}

# Cost model for the extraction the collected pages will trigger (FR-163).
# Deterministic adapters parse without an LLM (llm_fallback = False) and
# answer in 0.14-1.1 s; costing them at 8 s and 2,900 tokens per page made a
# 7,500-board plan display 16.7 h and 18.75 M tokens, which the budget check
# then refused - a correct plan rejected on an invented price.
PLAN_MAX_TOKENS = 6_000
PLAN_BATCH_SIZE = 3
TOKENS_PER_PAGE_IN = 2_500
TOKENS_PER_PAGE_OUT = 400
SECONDS_PER_PAGE_OVERHEAD = 4          # HTML and browser sources
SECONDS_PER_PAGE_OVERHEAD_API = 1      # JSON APIs (ATS, EURES, Arbeitnow)
DETERMINISTIC_ADAPTER_PREFIXES = (     # 0 LLM tokens in estimate()
    "ats.", "board.eures", "board.actiris", "board.arbeitnow",
    # The public JSON feeds (adapters/jobboards/_json_feed.py), all parsed
    # field by field with llm_fallback = False.
    "board.remotive", "board.remoteok", "board.jobicy", "board.himalayas",
    "board.hn_hiring", "board.adzuna",
)

# The review screen never renders a plan item by item: a Campaign A plan is
# ~7,500 one-page items, so the summary aggregates by adapter and hands out one
# page of rows at a time (FR-163).
PLAN_PAGE_SIZE = 100

_COUNTRY_BY_NAME = {
    "belgium": "BE", "belgie": "BE", "belgique": "BE", "belgië": "BE",
    "netherlands": "NL", "nederland": "NL", "holland": "NL", "the netherlands": "NL",
    "luxembourg": "LU", "luxemburg": "LU",
    "france": "FR", "germany": "DE", "deutschland": "DE", "duitsland": "DE",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "ireland": "IE", "spain": "ES", "portugal": "PT", "italy": "IT",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "finland": "FI",
    "poland": "PL", "czechia": "CZ", "austria": "AT", "switzerland": "CH",
    "united states": "US", "usa": "US", "canada": "CA",
}
_COUNTRY_GROUPS = {
    "benelux": ["BE", "NL", "LU"],
    "dach": ["DE", "AT", "CH"],
    "nordics": ["DK", "SE", "NO", "FI"],
}
_ISO2 = re.compile(r"^[A-Z]{2}$")

_KEYWORD_KEYS = (
    "roles", "target_roles", "titles", "job_titles", "functions", "function_families",
    "keywords", "role_families", "specialisations", "specializations", "domains",
)
_SKILL_KEYS = ("skills", "technologies", "tools", "core_competencies", "competencies")

#: Inside a *structured* entry, the fields that name the thing itself.  Same
#: reasoning as ``_PLACE_KEYS`` further down: a dream-job target role is stored
#: as ``{title, seniority, priority, rationale, source, quote}``, and flattening
#: every string in it turned the provenance into search terms - LinkedIn was
#: searched for "inferred", for the whole sentence explaining why the role was
#: chosen, and for the entry id "career_trajectory:1", while the real titles sat
#: at positions 8 to 21 and were never reached.
_TERM_KEYS = (
    "title", "titles", "example_titles", "family", "families",
    "name", "role", "roles", "label", "term", "text",
)

#: ``career_trajectory:1``, ``core_competencies:2`` - an entry's own id.
_ENTRY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*:\d+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass
class PlannedSource:
    """One source plan item before it is persisted (FR-162, FR-163)."""

    adapter_key: str
    native_query: dict[str, Any]
    rationale: str = ""
    estimated_pages: int = 1
    estimated_seconds: int = 0
    estimated_cost_eur: float = 0.0
    caps: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "adapter_key": self.adapter_key,
            "native_query": self.native_query,
            "rationale": self.rationale,
            "caps": self.caps,
            "estimated_pages": self.estimated_pages,
            "estimated_seconds": self.estimated_seconds,
            "estimated_cost_eur": round(self.estimated_cost_eur, 4),
        }


@dataclass
class SourceSelection:
    """The outcome of FR-164 source selection, rejections included."""

    selected: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def reject(self, entry: dict, reason: str, *, decline: dict | None = None) -> None:
        row = {
            "adapter_key": entry.get("adapter_key"),
            "display_name": entry.get("display_name"),
            "source_type": entry.get("source_type"),
            "reason": reason,
        }
        if decline is not None:
            # The machine-readable half of the reason: which source, declined
            # by whom, since when (FR-182, IR-101).
            row["declined"] = declines.blockers([decline])[0]
        self.rejected.append(row)


# ---------------------------------------------------------------------------
# Directive reading
# ---------------------------------------------------------------------------


def _walk(node: Any, keys: tuple[str, ...], out: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in keys:
                _flatten_strings(value, out)
            else:
                _walk(value, keys, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, keys, out)


def _flatten_strings(node: Any, out: list[str]) -> None:
    """Collect the strings that *name* something, ignoring what justifies it.

    A structured entry says what it is in one of :data:`_TERM_KEYS` and spends
    the rest of its fields on provenance - the rationale, the quote it was
    inferred from, the word "inferred", its own id.  Those are worth storing and
    worthless to search with, so an entry that names itself is read through its
    naming fields only.  A plain mapping names nothing, so it is walked whole,
    exactly as before.
    """
    if isinstance(node, str):
        text = node.strip()
        if text:
            out.append(text)
    elif isinstance(node, dict):
        named = [v for k, v in node.items() if str(k).lower() in _TERM_KEYS]
        for value in named or list(node.values()):
            _flatten_strings(value, out)
    elif isinstance(node, list):
        for value in node:
            _flatten_strings(value, out)


def _unique(values: list[str], limit: int | None = None) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen[key] = value.strip()
    result = list(seen.values())
    return result[:limit] if limit else result


def directive_json(directives: dict | None, do_not_disclose: set[str] | None = None) -> dict:
    """The directive set as nested JSON, ready to ground the prompt (FR-141..146).

    Directives are the job seeker's own instructions, but they still leave the
    machine, so do-not-disclose and special-category fields are dropped first
    (CR-410).  Under discretion mode the excluded employers are reduced to a
    count: the planner needs to know the list exists, not who is on it, and
    naming a current employer to the model is the one thing FR-385 exists to
    prevent.
    """
    if not directives:
        return {}
    out: dict[str, Any] = {}
    for column in ("job_content", "company_type", "location", "work_arrangement", "compensation"):
        value = from_json(directives.get(column), None)
        if value:
            out[column] = value
    if directives.get("notes_to_ai"):
        out["notes_to_ai"] = directives["notes_to_ai"]
    out["spontaneous_only"] = bool(directives.get("spontaneous_only"))
    out = redact(out, do_not_disclose)
    if directives.get("discretion_mode"):
        excluded = from_json(directives.get("discretion_excluded_companies"), []) or []
        out["discretion_mode"] = True
        out["excluded_company_count"] = len(excluded) if isinstance(excluded, list) else 0
    return out


def target_countries(directives: dict | None) -> list[str]:
    """ISO-2 countries this campaign targets (FR-164).

    The FR-144 location group is read *by name* - ``countries``,
    ``areas[*].country_code``, ``relocation_countries`` - and only then, for a
    directive set written by hand, by scanning the free-text labels.  Flattening
    the whole group into a bag of strings used to pick up ``commute_mode`` and
    ``place_type`` as if they were places.

    An empty result means "no geographic constraint stated", which lets a
    globally-covering source through but never a regional one from elsewhere.
    """
    location = from_json((directives or {}).get("location"), {}) or {}
    words = _location_words(location)
    countries: list[str] = []
    for word in words:
        text = word.strip()
        if _ISO2.match(text.upper()) and text.upper() not in {"EU", "UE"}:
            countries.append(text.upper())
            continue
        lowered = text.lower()
        if lowered in _COUNTRY_GROUPS:
            countries.extend(_COUNTRY_GROUPS[lowered])
        elif lowered in _COUNTRY_BY_NAME:
            countries.append(_COUNTRY_BY_NAME[lowered])
        else:
            for name, code in _COUNTRY_BY_NAME.items():
                if re.search(rf"\b{re.escape(name)}\b", lowered):
                    countries.append(code)
    return _unique(countries)


def campaign_keywords(
    directives: dict | None, dream: dict | None, composite: dict | None
) -> list[str]:
    """Role words to search with, taken only from what the user actually stated (CR-405)."""
    found: list[str] = []
    _walk(directive_json(directives), _KEYWORD_KEYS, found)
    for source in (dream, composite):
        if not source:
            continue
        for column in (
            "target_roles", "role_families", "responsibilities", "core_competencies",
            "adjacent_competencies", "domains",
        ):
            _flatten_strings(from_json(source.get(column), None), found)
    return _unique([t for t in found if _is_search_term(t)], 25)


def _is_search_term(text: str) -> bool:
    """Is this worth typing into a search box?

    Free-text directives and competency entries hold prose as well as names, and
    prose does not search: a board asked for "Design and build software
    hands-on, using careful object-oriented design" matches nothing, and the
    request is spent either way.  Kept deliberately blunt - length, sentence
    shape, and the id pattern - so it rejects what is obviously not a term and
    leaves every real title alone.
    """
    text = (text or "").strip()
    if not text or len(text) > 60 or len(text.split()) > 6:
        return False
    if _ENTRY_ID_RE.match(text):
        return False
    return not text.endswith(".")


def campaign_skills(composite: dict | None) -> list[str]:
    found: list[str] = []
    if composite:
        for column in ("core_competencies", "adjacent_competencies"):
            _flatten_strings(from_json(composite.get(column), None), found)
        _walk(from_json(composite.get("evidence_refs"), {}) or {}, _SKILL_KEYS, found)
    return _unique(found, 20)


# Keys of the FR-144 location group that name a *place*.  Everything else in
# that group - commute_mode, place_type, geocoded_at, radius_km - describes how
# the seeker travels or how the label was resolved, and naming a job board
# search after any of them ("jobs in car") is nonsense.
_PLACE_KEYS = ("label", "city", "cities", "regions", "locations", "places", "country_code")


def _location_words(location: dict | list | None) -> list[str]:
    """Place labels from the FR-144 location group, read by name."""
    if not isinstance(location, dict):
        return []
    words: list[str] = []
    for area in location.get("areas") or []:
        if isinstance(area, dict):
            for key in ("label", "country_code"):
                value = area.get(key)
                if isinstance(value, str) and value.strip():
                    words.append(value.strip())
        elif isinstance(area, str):
            words.append(area)
    home = location.get("home_location")
    if isinstance(home, dict) and isinstance(home.get("label"), str):
        words.append(home["label"].strip())
    for key in (*_PLACE_KEYS, "countries", "relocation_countries"):
        value = location.get(key)
        if isinstance(value, str) and value.strip():
            words.append(value.strip())
        elif isinstance(value, list):
            words.extend(v.strip() for v in value if isinstance(v, str) and v.strip())
    return [w for w in words if w]


def _locations(directives: dict | None) -> list[str]:
    """Place labels to search a location-capable source with (FR-144, FR-162)."""
    location = from_json((directives or {}).get("location"), {}) or {}
    words = [w for w in _location_words(location) if len(w) > 1 and not _ISO2.match(w.upper())]
    if not words:
        # An ISO-2 code is a usable location for a board that accepts one, and
        # is better than the empty string the boards were being handed.
        words = [w for w in _location_words(location) if _ISO2.match(w.upper())]
    return _unique(words, 8)


def _industries(directives: dict | None) -> list[str]:
    """FR-142 industry preferences, used to narrow source selection (FR-164)."""
    job_content = from_json((directives or {}).get("job_content"), {}) or {}
    if not isinstance(job_content, dict):
        return []
    values = job_content.get("industries_include") or job_content.get("industries") or []
    if isinstance(values, str):
        values = [values]
    return _unique([str(v) for v in values if str(v).strip()], 12)


def employers_and_schools(
    composite: dict | None, profile: dict | None
) -> tuple[list[str], list[str]]:
    """Seeds for the FR-165 alumni strategy, read from the profile only."""
    employers: list[str] = []
    schools: list[str] = []
    if composite:
        trajectory = from_json(composite.get("career_trajectory"), None)
        _walk(trajectory, ("company", "employer"), employers)
    sections = from_json((profile or {}).get("sections"), {}) or {}
    for key, value in sections.items() if isinstance(sections, dict) else []:
        lowered = key.lower()
        if "experience" in lowered or "position" in lowered:
            _walk(value, ("company", "employer", "organisation", "organization"), employers)
        if "education" in lowered or "school" in lowered:
            _walk(value, ("school", "institution", "university"), schools)
    return _unique(employers, 12), _unique(schools, 6)


# ---------------------------------------------------------------------------
# Source selection (FR-161, FR-164)
# ---------------------------------------------------------------------------


def select_sources(
    catalogue: list[dict],
    *,
    countries: list[str],
    spontaneous_only: bool = False,
    linkedin_allowed: bool = True,
    industries: list[str] | None = None,
    declined: dict[str, dict] | None = None,
) -> SourceSelection:
    """Keep only the sources whose catalogue metadata fits this search (FR-164).

    ``declined`` is the active decline map (FR-182, IR-101).  A declined
    adapter is rejected here, before it can be translated, charged a page or
    retried: the refusals that declined it are facts that do not change by
    asking again, and every campaign that ignored them re-issued the same
    refused requests.
    """
    selection = SourceSelection()
    wanted = {c.upper() for c in countries}
    industries = [i.lower() for i in (industries or [])]
    declined = declined or {}

    for entry in catalogue:
        decline = declined.get(str(entry.get("adapter_key") or ""))
        if decline is not None:
            selection.reject(
                entry, declines.rejection_reason(decline), decline=decline
            )
            continue
        if not entry.get("enabled", 1):
            selection.reject(entry, "disabled in the source catalogue")
            continue
        if entry.get("tos_status") == "prohibited" and not entry.get("acknowledged_at"):
            selection.reject(entry, "terms of service prohibit automated access (IR-101)")
            continue
        if entry.get("requires_ack") and not entry.get("acknowledged_at"):
            selection.reject(entry, "awaiting administrator acknowledgement (IR-101)")
            continue

        source_type = entry.get("source_type") or ""
        if spontaneous_only and source_type in ("job_board", "ats"):
            selection.reject(entry, "spontaneous-application mode skips vacancy sources (FR-149)")
            continue
        if source_type == "linkedin" and not linkedin_allowed:
            selection.reject(entry, "no consent recorded for LinkedIn automation (CR-403)")
            continue

        coverage = [c.upper() for c in (entry.get("coverage_countries") or [])]
        if wanted and coverage and not (wanted & set(coverage)):
            selection.reject(
                entry,
                "coverage " + "/".join(sorted(coverage)) + " does not include "
                + "/".join(sorted(wanted)),
            )
            continue

        covered_industries = [i.lower() for i in (entry.get("coverage_industries") or [])]
        if industries and covered_industries and not (set(industries) & set(covered_industries)):
            selection.reject(entry, "industry coverage does not match the directives")
            continue

        selection.selected.append(entry)
    return selection


# ---------------------------------------------------------------------------
# Native query generation (FR-162)
# ---------------------------------------------------------------------------


def load_prompt(path: Path = PROMPT_PATH) -> tuple[dict, str, str]:
    """Read a versioned prompt template: front matter, system part, user part (NFR-602)."""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    if text.startswith("---"):
        _, front, text = text.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
    system, _, user = text.partition("## User")
    system = system.replace("## System", "").strip()
    return meta, system, user.strip()


def render(template: str, values: dict[str, Any]) -> str:
    """``{{name}}`` substitution - JSON braces in the template stay untouched."""
    out = template
    for key, value in values.items():
        rendered = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        )
        out = out.replace("{{" + key + "}}", rendered)
    return out


# ---------------------------------------------------------------------------
# The adapter is the authority on its own query (FR-162, NFR-601)
# ---------------------------------------------------------------------------
#
# The planner used to invent a native-query vocabulary and hope the adapters
# read it.  They did not: ``keywords`` against ``queries``, ``location``
# against ``locations``, ``crawl_seeds`` against ``url``, ``board_slugs``
# against ``slug`` - one character apart in places, and silently ignored, so a
# source fetched nothing and the run still reported "done".  Every adapter
# already implements ``plan()`` and already writes the keys its own ``fetch()``
# reads, so that is now what produces the plan; the LLM enriches the result and
# the deterministic path below is normalised into the same dialect.

_adapters_loaded = False


def _load_adapters() -> None:
    """Import the adapter modules once so ``get_adapter`` can find them."""
    global _adapters_loaded
    if _adapters_loaded:
        return
    _adapters_loaded = True
    try:
        from dreamjob import adapters  # noqa: PLC0415 - lazy, avoids an import cycle

        adapters.discover()
    except Exception:  # noqa: BLE001 - planning must survive a broken adapter module
        log.exception("Adapter discovery failed; planning falls back to deterministic queries")


def adapter_for(adapter_key: str) -> SourceAdapter | None:
    """The adapter implementing a catalogue row, or ``None`` when none is registered."""
    _load_adapters()
    try:
        return get_adapter(adapter_key)
    except KeyError:
        return None
    except Exception:  # noqa: BLE001 - a broken adapter must not stop the plan
        log.exception("Adapter %s could not be constructed for planning", adapter_key)
        return None


def _adapter_unavailable(entry: dict) -> str | None:
    """Why this catalogue row's adapter cannot run, or ``None`` when it can."""
    adapter = adapter_for(entry.get("adapter_key") or "")
    if adapter is None:
        return None
    return adapter_unavailable_reason(adapter)


def adapter_plan_items(
    entry: dict, directives: dict, composite_profile: dict, caps: dict
) -> list[AdapterPlanItem] | None:
    """Ask a source to plan itself (FR-162).

    ``None`` means "there is no adapter to ask"; ``[]`` means "the adapter was
    asked and has nothing it can do with these directives" - which is a source
    to reject with a reason, not a source to plan with an empty query.
    """
    adapter = adapter_for(entry.get("adapter_key") or "")
    if adapter is None:
        return None
    try:
        items = adapter.plan(dict(directives), dict(composite_profile), dict(caps)) or []
    except Exception:  # noqa: BLE001 - one adapter must not stop the plan
        log.exception("[%s] plan() failed; falling back to a deterministic query",
                      entry.get("adapter_key"))
        return None
    return [
        item
        for item in items
        if isinstance(item, AdapterPlanItem)
        and isinstance(item.native_query, dict)
        and item.native_query
    ]


def _no_work_reason(entry: dict) -> str:
    """Why a source that cannot plan itself is left out of the plan (FR-164)."""
    source_type = entry.get("source_type") or ""
    name = entry.get("display_name") or entry.get("adapter_key")
    if source_type == "ats":
        return (
            f"no company with a known {name} board yet - the discovery sources have to run "
            "first, or name the company in the directives (FR-162)"
        )
    if source_type in ("website", "news"):
        return "no company website in scope yet - discovery has to run first (FR-162)"
    if source_type == "registry":
        return "no company with a legal identifier in scope yet (FR-162, DR-101)"
    if source_type == "events":
        return "no event calendar in scope and nothing to search it with (FR-162)"
    return f"{name} reports no work for these directives (FR-162)"


def _native_query_shape(entry: dict, sample: dict | None = None) -> dict:
    """What the LLM is told this source's query looks like (FR-162).

    Derived from the adapter's own plan item where there is one, so the shape
    advertised to the model is by construction the shape its ``fetch()`` reads.
    """
    if sample:
        return {k: _shape_of(v) for k, v in sample.items()}
    source_type = entry.get("source_type")
    shapes: dict[str, dict] = {
        "job_board": {"queries": ["str"], "locations": ["str"], "pages": 1, "max_records": 100},
        "ats": {"slug": "str", "company_name": "str", "max_records": 100},
        "directory": {"query": "str", "country": "str"},
        "registry": {"company": {"name": "str", "legal_id": "str"}, "years": 5},
        "website": {"url": "https://...", "max_pages": 12},
        "linkedin": {"search_urls": ["https://www.linkedin.com/search/results/..."]},
        "compensation": {"job_titles": ["str"], "region": "str"},
        "news": {"url": "https://...", "feeds": ["https://.../rss.xml"]},
        "events": {"url": "https://..."},
    }
    return shapes.get(source_type or "", {"query": "str"})


def _shape_of(value: Any) -> Any:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return 1
    if isinstance(value, list):
        return [_shape_of(value[0])] if value else ["str"]
    if isinstance(value, dict):
        return {k: _shape_of(v) for k, v in value.items()}
    return "str"


def _source_brief(entry: dict, per_source_pages: int, sample: dict | None = None) -> dict:
    return {
        "adapter_key": entry["adapter_key"],
        "display_name": entry.get("display_name"),
        "source_type": entry.get("source_type"),
        "coverage_countries": entry.get("coverage_countries") or "global",
        "query_capabilities": entry.get("query_capabilities") or {},
        "native_query_shape": _native_query_shape(entry, sample),
        "max_pages": per_source_pages,
    }


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def to_adapter_dialect(
    entry: dict,
    query: dict,
    *,
    keywords: list[str],
    locations: list[str],
    countries: list[str],
) -> dict:
    """Normalise a planner- or model-authored query into the adapter's keys (FR-162).

    Used only where the adapter could not plan itself.  It is deliberately
    additive: nothing the model wrote is removed, but the keys the adapters
    actually read are always present, so a source can never be handed a query
    it silently ignores.
    """
    out: dict[str, Any] = dict(query)
    source_type = entry.get("source_type") or ""
    words = _as_str_list(out.get("keywords")) or _as_str_list(out.get("queries")) or keywords
    places = _as_str_list(out.get("locations")) or _as_str_list(out.get("location")) or locations
    codes = _as_str_list(out.get("country_codes")) or _as_str_list(out.get("countries")) or countries

    if source_type in ("job_board", "linkedin", "compensation"):
        out.setdefault("queries", words or [""])           # HtmlBoardAdapter
        out.setdefault("locations", places)
        out.setdefault("keyword", words[0] if words else "")   # EuresAdapter
        out.setdefault("country_codes", codes)
    if source_type in ("directory", "registry"):
        out.setdefault("query", " OR ".join(words[:4]))
        out.setdefault("country", codes[0] if codes else "")
    if source_type in ("website", "news", "events"):
        seeds = _as_str_list(out.get("crawl_seeds")) or _as_str_list(out.get("start_urls"))
        if seeds and not out.get("url"):
            out["url"] = seeds[0]
    out.setdefault("countries", codes)
    return out


def fallback_query(
    entry: dict, keywords: list[str], locations: list[str], countries: list[str]
) -> dict:
    """Deterministic native query used when the LLM is unavailable (NFR-104).

    Written in the adapters' own vocabulary - ``queries``/``locations`` for an
    HTML board, ``keyword``/``country_codes`` for EURES - rather than in a
    vocabulary of the planner's own invention that no adapter reads.
    """
    source_type = entry.get("source_type")
    query: dict[str, Any] = {"keywords": keywords[:6] or ["*"]}
    if source_type not in ("job_board", "ats", "linkedin", "compensation"):
        query["query"] = " OR ".join(keywords[:4]) if keywords else ""
    query["location"] = locations[0] if locations else ""
    query["page"] = 1
    return to_adapter_dialect(
        entry, query, keywords=keywords, locations=locations, countries=countries
    )


# Keys that name the slice of a list an item reads.  A model answer is one
# answer for a whole source, so it can never speak for a particular slice.
_CURSOR_KEYS = frozenset({"page", "offset", "cursor", "start", "from"})


def discriminating_keys(items: list[AdapterPlanItem]) -> frozenset[str]:
    """The query keys that tell one of a source's own plan items from another.

    The model is asked once per *source*, but a source plans as many *items* as
    it has units of work: one per page for a paginated board, one per company
    website for the crawler, one per region x sector for an aggregator.  The
    keys those items differ in are the only thing keeping them separate work,
    so a single answer must not be allowed to write over them.

    It could, and it did.  ``website.crawl`` plans one item per company site;
    the model was shown that shape (:func:`_native_query_shape` samples the
    first item), answered with the single ``url`` the shape asked for, and
    :func:`enrich_native_query` stamped it onto every item.  One campaign wrote
    149 crawl items that all named ``https://dovane.be`` - one company crawled
    149 times, 148 companies never crawled - and nothing afterwards could tell
    the rows apart, because the only thing that had distinguished them was the
    field that got overwritten.

    Derived from the items rather than listed per adapter, so a source that
    starts partitioning itself on a new key is protected without an edit here.
    """
    if len(items) < 2:
        return frozenset()
    queries = [i.native_query or {} for i in items]
    marks: dict[str, str] = {}
    varying: set[str] = set()
    for query in queries:
        for key, value in query.items():
            mark = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            if marks.setdefault(key, mark) != mark:
                varying.add(key)
    # A key some items carry and others do not separates them just as surely.
    shared = set.intersection(*(set(q) for q in queries))
    return frozenset(varying | (set(marks) - shared))


def enrich_native_query(
    native: dict, generated: dict | None, protected: frozenset[str] = frozenset()
) -> dict:
    """Let the model refine an adapter-authored query without re-shaping it.

    The adapter owns the key set: a model answer may only replace a value the
    adapter already asked for, and only with a value of the same kind.  That
    keeps FR-162's "the LLM translates into the source's native query" true
    while making it impossible for a model answer to produce a query the
    adapter cannot execute.

    ``protected`` names the keys this source varies from one item to the next
    (see :func:`discriminating_keys`), plus the cursor keys no per-source answer
    can speak for.  Those are the adapter's alone: one answer covering twenty
    items has nothing to say about which page the seventh of them reads.
    """
    if not generated:
        return dict(native)
    out = dict(native)
    blocked = protected | _CURSOR_KEYS
    for key, value in (generated or {}).items():
        if key in blocked or key not in out or value in (None, "", [], {}):
            continue
        current = out[key]
        if isinstance(current, bool) or isinstance(value, bool):
            continue
        if isinstance(current, list) and isinstance(value, list):
            out[key] = [v for v in value if isinstance(v, (str, int, float))] or current
        elif isinstance(current, str) and isinstance(value, str):
            out[key] = value
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)):
            out[key] = type(current)(value)
    return out


def generate_native_queries(
    llm: LLMClient,
    entries: list[dict],
    *,
    directives: dict,
    dream: dict,
    composite_public: dict,
    caps: dict,
    per_source_pages: int,
    samples: dict[str, dict | None] | None = None,
) -> dict[str, dict]:
    """Generate every selected source's native query (FR-162).

    Sources are planned in small batches: a reasoning model spends its output
    budget thinking, and a batch that is too large returns nothing at all.
    Batching also means a failed call costs only its own sources, which fall
    back to the deterministic query below instead of losing the whole plan.
    """
    out: dict[str, dict] = {}
    for start in range(0, len(entries), PLAN_BATCH_SIZE):
        batch = entries[start : start + PLAN_BATCH_SIZE]
        keys = [e["adapter_key"] for e in batch]
        # Translating a catalogue entry into its native query is a structured
        # rewrite, not an open-ended analysis: the cheap model answers it
        # reliably in JSON mode, while the configured reasoning model spends
        # its whole output budget deliberating and returns nothing.  The strong
        # model is therefore the fallback, not the first attempt.
        for prefer_strong in (False, True):
            try:
                planned = _plan_batch(
                    llm,
                    batch,
                    directives=directives,
                    dream=dream,
                    composite_public=composite_public,
                    caps=caps,
                    per_source_pages=per_source_pages,
                    prefer_strong=prefer_strong,
                    samples=samples,
                )
            except BudgetExhausted:
                raise
            except (LLMError, ValueError, KeyError, TypeError) as exc:
                log.warning("Planning batch %s failed on attempt %s: %s", keys, prefer_strong, exc)
                continue
            # An answer with no plans in it is an *answer*: the model looked at
            # these sources and had nothing to add.  Escalating to the reasoning
            # model then spends its whole output ceiling to be told the same
            # thing, so escalation is reserved for a call that actually failed.
            out.update(planned)
            if not planned:
                log.info("The model returned no query for %s; the adapter's own plan stands", keys)
            break
        else:
            log.warning("Sources %s fall back to deterministic queries", keys)
    return out


# The model may name the list differently from one run to the next.
_PLAN_LIST_KEYS = ("plans", "plan", "plan_entries", "entries", "sources", "results", "items")


def _plan_batch(
    llm: LLMClient,
    entries: list[dict],
    *,
    directives: dict,
    dream: dict,
    composite_public: dict,
    caps: dict,
    per_source_pages: int,
    prefer_strong: bool | None = None,
    samples: dict[str, dict | None] | None = None,
) -> dict[str, dict]:
    """One LLM call for a handful of sources.

    The directives are the job seeker's own instructions and stay in the
    instruction channel; the composite profile is derived from uploaded and
    enriched material and is passed as untrusted data (NFR-205).
    """
    meta, system, user_template = load_prompt()
    briefs = [
        _source_brief(e, per_source_pages, (samples or {}).get(e["adapter_key"]))
        for e in entries
    ]
    user = render(
        user_template,
        {"directives": directives, "dream_job": dream, "sources": briefs, "caps": caps},
    )
    data = llm.complete_json(
        "plan.campaign",
        system=system,
        user=user,
        untrusted={"composite_profile": json.dumps(composite_public, ensure_ascii=False)},
        schema_hint=(
            '{"plans": [{"adapter_key": str, "native_query": object, "rationale": str, '
            '"estimated_pages": int}]}'
        ),
        prompt_template=meta.get("id", "campaign_plan"),
        prompt_version=meta.get("version", "1"),
        entity_type="campaign",
        entity_id=llm.campaign_id,
        # The reasoning model returns an empty answer when it is also given a
        # json_object response format, so JSON is requested in the prompt and
        # parsed from the text.  The ceiling covers reasoning plus answer.
        prefer_strong=prefer_strong,
        json_mode=prefer_strong is False,
        max_tokens=PLAN_MAX_TOKENS,
    )
    return _read_plans(data, {e["adapter_key"] for e in entries}, per_source_pages)


def _read_plans(data: Any, known: set[str], per_source_pages: int) -> dict[str, dict]:
    """Validate the model's answer before any of it is persisted (NFR-205).

    Raises ``ValueError`` when the answer carries no plan list at all - that is
    a failed call, and the only thing worth escalating to the strong model.
    """
    plans: Any = data
    if isinstance(data, dict):
        plans = next((data[k] for k in _PLAN_LIST_KEYS if isinstance(data.get(k), list)), None)
        if plans is None:
            lists = [v for v in data.values() if isinstance(v, list)]
            if len(lists) != 1:
                raise ValueError("the model answered with no list of plans")
            plans = lists[0]
    elif not isinstance(plans, list):
        raise ValueError(f"the model answered with {type(data).__name__}, not a list of plans")
    out: dict[str, dict] = {}
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        key = plan.get("adapter_key")
        if key not in known:
            log.info("Discarding plan for unknown adapter %r", key)
            continue
        native = plan.get("native_query")
        if not isinstance(native, dict) or not native:
            continue
        out[key] = {
            "native_query": native,
            "rationale": str(plan.get("rationale") or "")[:1000],
            "estimated_pages": _clamp_int(plan.get("estimated_pages"), 1, per_source_pages),
        }
    return out


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


# ---------------------------------------------------------------------------
# Estimation (FR-163)
# ---------------------------------------------------------------------------


def is_deterministic(adapter_key: str) -> bool:
    """Does this source parse without an LLM? (FR-163, section 6.9)

    ATS boards, EURES, Actiris and Arbeitnow answer JSON, XML or sitemaps that
    the adapters read field by field with ``llm_fallback = False``.  Running an
    extraction model over them would spend 25 M tokens to reproduce what the
    parser already has, so nothing plans, budgets or bills for one.
    """
    return str(adapter_key or "").startswith(DETERMINISTIC_ADAPTER_PREFIXES)


def estimate(entry: dict, pages: int) -> tuple[int, float]:
    """Duration in seconds and cost in EUR for fetching ``pages`` from a source.

    Two prices, not one: a JSON API answers in about a second and needs no
    extraction model, while an HTML page has to be rendered, cleaned and often
    read by one.  Charging every source the HTML price put a 7,500-board plan at
    16.7 hours and 18.75 M tokens on the review screen and tripped the budget
    check before the campaign could run (FR-163, FR-186).
    """
    settings = get_settings()
    rps = float(entry.get("rate_limit_rps") or settings.per_domain_rps or 0.5)
    access_method = entry.get("access_method")
    overhead = (
        SECONDS_PER_PAGE_OVERHEAD_API if access_method == "api" else SECONDS_PER_PAGE_OVERHEAD
    )
    per_page = (1.0 / rps if rps > 0 else 2.0) + overhead
    if access_method == "browser":
        per_page += (settings.browser_min_delay_ms + settings.browser_max_delay_ms) / 2000.0
    seconds = int(pages * per_page)
    api_cost = pages * float(entry.get("cost_per_call_eur") or 0.0)
    llm_cost = 0.0 if is_deterministic(entry.get("adapter_key")) else pages * (
        TOKENS_PER_PAGE_IN / 1e6 * settings.llm_cost_per_1m_input_eur
        + TOKENS_PER_PAGE_OUT / 1e6 * settings.llm_cost_per_1m_output_eur
    )
    return seconds, round(api_cost + llm_cost, 4)


def allocate_pages(planned: list[PlannedSource], max_pages: int) -> dict[str, int]:
    """Fit the plan inside the campaign's own page budget (FR-186).

    ``max_pages`` bounds the *whole run*, but nothing ever compared the plan
    against it: a 19-source plan could ask for 50 pages under a 20-page cap, and
    collection then spent the budget in plan order, so the sources at the end
    were recorded as "not started: max_pages" having never been asked for
    anything.  Every source keeps a floor of one page and the surplus is shared
    proportionally, so a cap shortens the plan instead of deleting its tail.
    """
    if not planned or max_pages <= 0:
        return {"requested": 0, "granted": 0, "max_pages": max_pages}
    wants = [max(1, int(item.estimated_pages or 1)) for item in planned]
    requested = sum(wants)
    if requested > max_pages:
        if max_pages < len(planned):
            # More sources than the cap allows pages: handing one page to every
            # item granted more than max_pages.  Give the pages to the hungriest
            # sources and zero to the rest so the cap holds.
            order = sorted(range(len(planned)), key=lambda index: -wants[index])
            for item in planned:
                item.estimated_pages = 0
            for index in order[:max_pages]:
                planned[index].estimated_pages = 1
        else:
            floor = 1
            surplus = max(0, max_pages - floor * len(planned))
            over_floor = max(1, requested - floor * len(planned))
            for item, want in zip(planned, wants, strict=True):
                item.estimated_pages = floor + int((want - floor) * surplus / over_floor)
            # Integer division leaves a remainder; give it to the hungriest sources.
            granted = sum(item.estimated_pages for item in planned)
            for want, item in sorted(
                zip(wants, planned, strict=True), key=lambda pair: -pair[0]
            ):
                if granted >= max_pages:
                    break
                if item.estimated_pages < want:
                    item.estimated_pages += 1
                    granted += 1
    granted = sum(item.estimated_pages for item in planned)
    for item in planned:
        # The reuse assessment (FR-342) measures against the budget the plan
        # actually holds, so record it after the cap has been applied.
        item.caps["planned_pages"] = item.estimated_pages
    return {"requested": requested, "granted": granted, "max_pages": max_pages}


# ---------------------------------------------------------------------------
# LinkedIn network strategy (FR-165)
# ---------------------------------------------------------------------------


def linkedin_network_plan(
    *,
    adapter_key: str,
    keywords: list[str],
    employers: list[str],
    schools: list[str],
    locations: list[str],
    caps: dict,
) -> PlannedSource:
    """First-degree, similar-role and alumni crawling under an explicit cap (FR-165)."""
    max_profiles = int(caps.get("max_people") or DEFAULT_NETWORK_CAPS["max_profiles"])
    max_companies = int(caps.get("max_companies") or DEFAULT_NETWORK_CAPS["max_companies"])
    max_profiles = min(max_profiles, DEFAULT_NETWORK_CAPS["max_profiles"] * 4)
    # FR-165 caps this crawl on its own terms.  ``max_companies`` is the
    # campaign-wide ceiling and now stands at 8,000 board-derived companies
    # (FR-186); letting it through here would ask a browser-driven, intrusive
    # crawl to map 8,000 companies because a JSON board sweep was made bigger.
    max_companies = min(max_companies, DEFAULT_NETWORK_CAPS["max_companies"] * 4)
    titles = keywords[:5]
    facets = [
        {"facet": "first_degree", "description": "existing first-degree contacts"},
        {
            "facet": "similar_roles",
            "description": "people holding the target roles",
            "titles": titles,
        },
        {
            "facet": "alumni_employers",
            "description": "alumni of former employers",
            "seeds": employers,
        },
        {"facet": "alumni_schools", "description": "alumni of the same schools", "seeds": schools},
    ]
    # One search page per title reached about fifty profiles and stopped there,
    # however many the campaign's own cap allowed: ``pages`` below was computed
    # and never used.  Spread the profile cap over the titles at roughly ten
    # results a page, up to the source's ten-page ceiling, and each page after
    # the first is requested with LinkedIn's own ``page`` parameter.
    per_title = max(
        1, min(10, -(-max_profiles // (10 * max(1, len(titles)))))
    )
    search_urls: list[str] = []
    for title in titles:
        encoded = re.sub(r"\s+", "%20", title)
        for page in range(1, per_title + 1):
            suffix = "" if page == 1 else f"&page={page}"
            search_urls.append(
                "https://www.linkedin.com/search/results/people/?keywords="
                f"{encoded}{suffix}"
            )
    native_query = {
        "strategy": "network",
        "facets": facets,
        "search_urls": search_urls,
        "locations": locations,
        "max_profiles": max_profiles,
        "max_companies": max_companies,
    }
    pages = max(1, len(search_urls))
    return PlannedSource(
        adapter_key=adapter_key,
        native_query=native_query,
        rationale=(
            f"Map up to {max_profiles} profiles and {max_companies} companies through "
            "first-degree contacts, people in the target roles and alumni of the same "
            "employers and schools (FR-165)."
        ),
        estimated_pages=pages,
        estimated_seconds=pages * 45,
        estimated_cost_eur=0.0,
        caps={"max_profiles": max_profiles, "max_companies": max_companies},
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def resolve_caps(campaign: dict, directives: dict | None) -> dict:
    """Campaign caps: defaults, then anything the user set (FR-186).

    The FR-142 job-content group has no cap fields, so the directive set can
    only state a cap under an explicit ``caps`` object; ``campaign.caps`` - what
    the API and the review screen write - wins over both.
    """
    caps = dict(DEFAULT_CAPS)
    stated = from_json((directives or {}).get("job_content"), {}) or {}
    if isinstance(stated, dict) and isinstance(stated.get("caps"), dict):
        for key, value in stated["caps"].items():
            if key in caps and isinstance(value, (int, float)) and not isinstance(value, bool):
                caps[key] = int(value)
    existing = campaign.get("caps") or {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(value, (int, float)):
                caps[key] = int(value)
    return caps


def _adapter_directives(
    directives: dict,
    keywords: list[str],
    locations: list[str],
    countries: list[str],
) -> dict:
    """The directive payload an adapter's own ``plan()`` reads (FR-162).

    Adapters read ``job_content.titles``/``keywords`` and ``location.cities``/
    ``countries``.  The planner already resolves both, so they are written into
    the payload under the names the adapters look for rather than left for each
    adapter to re-derive - and the payload is the redacted one, so nothing
    marked do-not-disclose reaches a source query either (CR-410).
    """
    payload = dict(directives)
    job_content = dict(payload.get("job_content") or {})
    job_content.setdefault("titles", keywords[:8])
    existing = job_content.get("keywords")
    job_content["keywords"] = _unique([*_as_str_list(existing), *keywords], 12)
    payload["job_content"] = job_content
    location = dict(payload.get("location") or {})
    if locations:
        location["cities"] = _unique([*_as_str_list(location.get("cities")), *locations], 8)
    if countries:
        location["countries"] = countries
    payload["location"] = location
    return payload


def _adapter_caps(
    caps: dict,
    *,
    companies: list[dict],
    keywords: list[str],
    locations: list[str],
    per_source_pages: int,
) -> dict:
    """The caps payload an adapter's own ``plan()`` reads (FR-162, FR-186).

    ``companies`` is the inventory that turns a company-scoped source from a
    silent no-op into real work: an ATS board slug, a website to crawl, a
    registry identity to look up.
    """
    sites = [
        {
            "url": _home_url(company),
            "company_id": company.get("id"),
            "company_name": company.get("name"),
        }
        for company in companies
        if _home_url(company)
    ]
    return {
        **caps,
        "max_pages_per_source": per_source_pages,
        "companies": companies,
        "company_sites": sites,
        "sites": sites,
        "keywords": keywords,
        "location": locations[0] if locations else "",
        "locations": locations,
    }


def _home_url(company: dict) -> str:
    domain = str(company.get("domain") or "").strip()
    if domain:
        return domain if domain.startswith("http") else f"https://{domain}"
    careers = str(company.get("careers_url") or "").strip()
    return careers if careers.startswith("http") else ""


def _collection_targets(
    inputs: dict, directives: dict, countries: list[str] | None = None
) -> list[dict]:
    """Companies this campaign may collect against (FR-143, FR-162, FR-164).

    Two sources, in this order: the companies the job seeker named in the
    FR-143 directives, and the companies the knowledge base already holds - the
    second is what makes a company found by discovery readable by the harvest
    stage, in this run or in the next campaign.

    ``countries`` is the campaign's target geography.  A company whose country
    is known and outside it is dropped: the knowledge base is shared and global,
    and planning an ATS board for every company in it is how a Brussels search
    came to fetch jobs from Oslo and Ohio.  A company whose country is unknown
    is kept, because there is nothing to judge it by; its postings are filtered
    on their own location when they become opportunities.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def add(company: dict) -> None:
        key = str(
            company.get("id")
            or company.get("domain")
            or company.get("legal_id")
            or company.get("name")
            or ""
        ).strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(company)

    company_type = from_json(directives.get("company_type"), {}) or {}
    if isinstance(company_type, dict):
        for named in company_type.get("include_companies") or []:
            if isinstance(named, dict) and named.get("name"):
                # A company the seeker named explicitly is always in scope.
                add(
                    {
                        "name": named.get("name"),
                        "domain": named.get("domain"),
                        "legal_id": named.get("legal_id"),
                        "ats_vendor": named.get("ats_vendor"),
                        "ats_slug": named.get("ats_slug"),
                    }
                )

    in_scope = {c.upper() for c in (countries or []) if c}
    for row in inputs.get("companies_in_scope") or []:
        if not (isinstance(row, dict) and row.get("name")):
            continue
        if in_scope and not _company_in_scope(row, in_scope):
            continue
        add(dict(row))
    # Notable employers are planned first (FR-162). A hint only: the company is
    # still profiled from its own site like any other, and a list that does not
    # cover the country changes nothing.
    return top_employers.prioritise(out, countries)


def _company_in_scope(row: dict, in_scope: set[str]) -> bool:
    """Is a knowledge-base company plausibly inside the target geography?

    Known and in scope: yes.  Known and outside: no.  Unknown: yes - the
    knowledge base does not record a country for most companies, and dropping
    every one of them would silently retire the ATS sources altogether.  Their
    postings are filtered on the posting's own country at synthesis.
    """
    country = str(row.get("country") or "").strip().upper()
    if not country:
        return True
    return country in in_scope


def generate_plan(
    campaign_id: str,
    job_seeker_id: str,
    *,
    use_llm: bool = True,
    llm: LLMClient | None = None,
    assess_knowledge_base: bool = True,
    max_reuse_age_days: int | None = None,
) -> dict:
    """Produce, persist and summarise a campaign plan (FR-161..166, FR-342).

    ``max_reuse_age_days`` is handed to the knowledge-base reuse assessment: a
    source or target whose last successful collection is older than that many
    days is re-opened as fresh plan work - its row back to ``planned`` with
    clean counters - instead of reused, so a periodic run finds what appeared
    since the last pass while still reusing what is genuinely fresh (FR-342).
    ``None`` keeps the staleness policy alone.
    """
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    if campaign.get("status") == "running":
        # Re-planning deletes the plan items collected records point back to
        # (FR-166), so it waits until collection has stopped.
        raise ValueError("Pause or cancel the running collection before re-planning")

    inputs = repo.load_planning_inputs(campaign)
    directives = inputs["directives"] or {}
    composite = inputs["composite_profile"] or {}
    dream = inputs["dream_job_model"] or {}
    profile = inputs["profile_version"] or {}

    countries = target_countries(directives)
    if not countries:
        # A search that names no geography still has one: the companies it is
        # about.  Inferring it here keeps the registries constrained - without
        # it, `select_sources` treated an empty wanted-set as "everything
        # matches" and planned 379 US SEC EDGAR lookups for an all-Belgian
        # corpus, every one of which resolved no CIK (NFR-403).
        countries = repo.dominant_company_countries()
    keywords = campaign_keywords(directives, dream, composite)
    locations = _locations(directives)
    industries = _industries(directives)
    caps = resolve_caps(campaign, directives)
    linkedin_allowed = repo.has_consent(job_seeker_id, "linkedin_automation")

    # The full catalogue is read once, because IR-101 needs the rows the
    # enabled view hides: a source whose terms prohibit access, or that waits
    # for an acknowledgement, is disabled *and* requires_ack, so it is exactly
    # the row a plan must be able to decline and list for the administrator.
    catalogue_all = repo.list_catalogue(enabled_only=False)
    # FR-182 / IR-101: a source refused across requests or across campaigns is
    # declined, and declared so before selection rather than rediscovered by
    # every run.  Terms are declined here, before a request is ever charged.
    for entry in catalogue_all:
        if entry.get("acknowledged_at"):
            continue
        if entry.get("tos_status") == "prohibited":
            declines.record_terms_decline(
                entry["adapter_key"],
                reason=declines.REASON_TERMS,
                detail=(
                    f"{entry.get('display_name') or entry['adapter_key']}: "
                    "terms of service prohibit automated access (IR-101)"
                ),
            )
        elif entry.get("requires_ack"):
            declines.record_terms_decline(
                entry["adapter_key"],
                reason=declines.REASON_UNACKNOWLEDGED,
                detail=(
                    f"{entry.get('display_name') or entry['adapter_key']}: "
                    "the terms await an administrator's acknowledgement (IR-101)"
                ),
            )
    # Read once per plan: the map cannot change under a plan already being
    # written, and selection, the blocker check and the summary all read it.
    active_declines = declines.active_map()
    catalogue = [
        entry
        for entry in catalogue_all
        if entry.get("enabled", 1)
        and (not entry.get("requires_ack") or entry.get("acknowledged_at"))
    ]
    selection = select_sources(
        catalogue,
        countries=countries,
        spontaneous_only=bool(directives.get("spontaneous_only")),
        linkedin_allowed=linkedin_allowed,
        industries=industries,
        declined=active_declines,
    )

    per_source_pages = int(caps.get("max_pages_per_source", 20))
    public_directives = directive_json(directives, inputs["do_not_disclose"])
    public_composite = _public_composite(composite, inputs["do_not_disclose"])

    # FR-162, pass 0: materialise the targets before anything is translated.
    # A source is not a unit of work - a board is, a region-and-sector slice of
    # an aggregator is - and until this ran, every company-scoped source was
    # planned once with an empty target list and collected nothing.
    #
    # The inventory is re-read scoped to the campaign's geography.  It is
    # ordered by recency and capped, so filtering it afterwards would only ever
    # see the most-recently-touched companies anywhere in the world; reading it
    # scoped is what makes the cap mean "the 200 in-scope companies".
    # FR-143/FR-164: draw the universe from the registry when it is staged.
    # Until this ran, the only companies a campaign could consider were the
    # ones a previous scrape had happened to find - which excludes every
    # company that has never advertised, the whole population a speculative
    # opening is drawn from. It is a no-op when nothing is staged, and it only
    # promotes the in-scope slice, so a two-million-row register becomes a few
    # hundred company rows rather than two million.
    try:
        from dreamjob.pipeline import kbo_bulk  # noqa: PLC0415

        seeded = kbo_bulk.select_for_directives(
            directives,
            limit=min(int(caps.get("max_companies") or 60), 200),
            country=(countries or ["BE"])[0],
        )
        if seeded.get("materialised"):
            log.info(
                "Registry seed contributed %d companies for divisions %s",
                seeded["materialised"],
                seeded.get("divisions") or "(all)",
            )
    except Exception:  # noqa: BLE001 - a registry is a source, not a dependency
        log.exception("Registry seed selection failed; planning from the knowledge base alone")

    if countries:
        inputs["companies_in_scope"] = repo.companies_in_scope(countries=countries)
    companies = _collection_targets(inputs, directives, countries)
    found = discovery.discover(
        selected=selection.selected,
        companies=companies,
        countries=countries,
        caps=caps,
        adapter_for=adapter_for,
    )
    plan_directives = _adapter_directives(public_directives, keywords, locations, countries)
    plan_caps = _adapter_caps(
        caps,
        companies=companies,
        keywords=keywords,
        locations=locations,
        per_source_pages=per_source_pages,
    )
    if found.boards:
        # The per-vendor slug list every ATS adapter's own ``company_targets()``
        # reads.  Boards travel here and not in ``companies`` on purpose: a
        # board is a route to a vacancy list, not a company identity, and
        # thousands of them in the company inventory would have the registry
        # adapters plan a filing lookup for each (FR-162, section 2.8).
        plan_caps["ats"] = found.boards
    adapter_items: dict[str, list[AdapterPlanItem] | None] = {
        entry["adapter_key"]: found.items.get(entry["adapter_key"])
        or adapter_plan_items(entry, plan_directives, public_composite, plan_caps)
        for entry in selection.selected
    }

    # A source that was asked and reported no work is left out, with the reason
    # shown on the review screen.  It used to be planned with an empty query and
    # then recorded as "done, 0 records, 0 errors" - a failure dressed as a run.
    #
    # A source whose adapter is unavailable in this deployment - a registry
    # whose API key is not configured - is left out here, before it is charged
    # anything.  Its own ``plan()`` already answers ``[]`` for that, but a
    # keyword-searchable source used to survive on the fall-back query alone,
    # so every collection run retried one unusable item per plan, logged an
    # ERROR with a traceback per page and counted the key's absence as a
    # collection fault (FR-245, NFR-403).
    runnable: list[dict] = []
    for entry in selection.selected:
        unavailable = _adapter_unavailable(entry)
        if unavailable:
            log.info("[%s] not planned: %s", entry["adapter_key"], unavailable)
            selection.reject(entry, unavailable)
            continue
        items = adapter_items.get(entry["adapter_key"])
        if items or items is None or _keyword_searchable(entry):
            runnable.append(entry)
        else:
            selection.reject(entry, _no_work_reason(entry))
    selection.selected = runnable

    native_by_key: dict[str, dict] = {}
    llm_used = False
    degraded_reason: str | None = None

    # A source whose targets discovery evidenced has nothing to translate: the
    # query *is* the board slug or the partition, and asking a model to rewrite
    # it would spend tokens to risk a hallucinated slug (section 6.7).  Only the
    # keyword-shaped sources are sent, which is why a 7,500-item plan costs
    # 10-20k tokens instead of 18.75 M.
    translatable = [
        e for e in selection.selected if e["adapter_key"] not in found.deterministic_keys
    ]
    if translatable and use_llm:
        client = llm or LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)
        if client.budget.should_degrade():
            degraded_reason = "token budget nearly exhausted; deterministic queries used (NFR-104)"
        else:
            try:
                native_by_key = generate_native_queries(
                    client,
                    translatable,
                    directives=public_directives,
                    dream=_public_dream(dream, inputs["do_not_disclose"]),
                    composite_public=public_composite,
                    caps=caps,
                    per_source_pages=per_source_pages,
                    samples={
                        key: (items[0].native_query if items else None)
                        for key, items in adapter_items.items()
                    },
                )
                llm_used = bool(native_by_key)
                if len(native_by_key) < len(translatable):
                    degraded_reason = (
                        f"{len(translatable) - len(native_by_key)} of "
                        f"{len(translatable)} sources fell back to deterministic queries"
                    )
            except BudgetExhausted as exc:
                degraded_reason = f"token budget exhausted: {exc} (NFR-104)"
            except (LLMError, ValueError, KeyError) as exc:
                log.warning("Falling back to deterministic queries: %s", exc)
                degraded_reason = f"LLM unavailable: {exc}"

    planned: list[PlannedSource] = []
    for entry in selection.selected:
        generated = native_by_key.get(entry["adapter_key"])
        items = adapter_items.get(entry["adapter_key"])
        area = "/".join(countries) or "the stated geography"
        if items:
            # One answer for the whole source may refine what every item of it
            # shares, and nothing that separates them (FR-162).
            protected = discriminating_keys(items)
            for item in items:
                native_query = enrich_native_query(
                    item.native_query, (generated or {}).get("native_query"), protected
                )
                # The adapter's own page count wins over the model's.  The
                # model answers once for the whole source, so applying its
                # number per item multiplied it by however many items the
                # adapter had planned: a four-page estimate for Actiris became
                # four pages each across twenty items, eighty page-fetches of a
                # board that holds twenty.
                pages = _clamp_int(
                    item.estimated_pages or (generated or {}).get("estimated_pages"),
                    1,
                    per_source_pages,
                )
                planned.append(
                    _planned_source(entry, native_query, pages, item.rationale
                                    or (generated or {}).get("rationale") or area)
                )
            continue
        if generated:
            native_query = to_adapter_dialect(
                entry,
                generated["native_query"],
                keywords=keywords,
                locations=locations,
                countries=countries,
            )
            rationale = generated["rationale"] or f"Selected from the source catalogue for {area}."
            pages = generated["estimated_pages"]
        else:
            native_query = fallback_query(entry, keywords, locations, countries)
            rationale = (
                f"{entry.get('display_name') or entry['adapter_key']} covers "
                f"{'/'.join(countries) or 'the requested area'}; deterministic keyword query."
            )
            pages = min(per_source_pages, 3)
        native_query.setdefault("countries", countries)
        for query in one_target_each(entry, native_query):
            source = _planned_source(entry, query, pages, rationale)
            if items == []:
                # The adapter could not plan itself, so this query is the
                # planner's guess: say so rather than let collection report an
                # empty run.
                source.caps["unverified_route"] = True
            planned.append(source)

    # FR-165: the network crawl is a *browser strategy*, not an adapter.  It is
    # deliberately absent from the registry and from the source catalogue
    # (``campaigns.BROWSER_STRATEGY_KEYS``), so requiring a catalogue row here
    # was requiring a row nothing ever writes: the crawl was never planned, the
    # browser session's allowlist was therefore always empty, and the run
    # planner's "Start run" could never leave its disabled state.  The seeker's
    # CR-401 consent is the gate, and it is the only one there should be.
    #
    # The item therefore stays in the plan even though no *collection* adapter
    # can serve it: it is the browser run's target allowlist (FR-205), built by
    # ``browser.linkedin.linkedin_plan_items``.  Collection recognises the key
    # as browser strategy and skips it once per plan, at INFO, without an error
    # (``BROWSER_STRATEGY_KEYS``); removing the item here would leave the
    # browser run with nothing to crawl.
    if linkedin_allowed:
        network_entry = next(
            (e for e in selection.selected if e.get("source_type") == "linkedin"), None
        )
        employers, schools = employers_and_schools(composite, profile)
        network = linkedin_network_plan(
            adapter_key=(network_entry or {}).get("adapter_key") or LINKEDIN_NETWORK_ADAPTER,
            keywords=keywords,
            employers=employers,
            schools=schools,
            locations=locations,
            caps=caps,
        )
        network.caps.setdefault("stage", repo.STAGE_DEEPEN)
        planned.append(network)

    # FR-182 / FR-186: a plan with no runnable source is a blocker, not a
    # campaign.  A real run planned its vacancy sources, met 403 on every one
    # of them at collection time, and spent its whole page budget on refusals
    # nobody could act on; the user got an empty shortlist and no statement of
    # why.  When every in-scope source is declined, planning stops here with
    # the names and reasons instead of letting a launch charge refusals again.
    #
    # Scope is read from the catalogue rather than from the selection, because
    # the declined sources a plan must report include the ones IR-101 hides
    # from selection altogether.
    wanted = {c.upper() for c in countries}
    catalogue_by_key = {entry["adapter_key"]: entry for entry in catalogue_all}
    declined_in_scope: list[dict] = []
    for key, row in active_declines.items():
        entry = catalogue_by_key.get(key, {})
        coverage = [c.upper() for c in (entry.get("coverage_countries") or [])]
        if wanted and coverage and not (wanted & set(coverage)):
            continue
        if (entry.get("source_type") == "linkedin") and not linkedin_allowed:
            continue
        declined_in_scope.append(row)
    collection_planned = [
        source for source in planned if source.adapter_key not in repo.BROWSER_STRATEGY_KEYS
    ]
    if not collection_planned and declined_in_scope:
        blocked = declines.blockers(declined_in_scope)
        repo.record_audit(
            "campaign.plan_blocked",
            job_seeker_id=job_seeker_id,
            entity_type="campaign",
            entity_id=campaign_id,
            detail={"reason": "no_usable_sources", "blockers": blocked},
        )
        raise NoUsableSources(
            declines.no_sources_message(blocked, what="plan"), blockers=blocked
        )

    budget = allocate_pages(planned, int(caps.get("max_pages") or 0))
    # A Campaign A plan holds thousands of items; scanning the catalogue for
    # each one is quadratic work on the review screen's critical path.
    by_key = {entry["adapter_key"]: entry for entry in catalogue}
    for source in planned:
        source.estimated_seconds, source.estimated_cost_eur = estimate(
            by_key.get(source.adapter_key, {}), source.estimated_pages
        )

    # FR-186: the scale-down is a fact about the plan, so it is stored with the
    # campaign and read back by the plan screen and the run report - not only
    # logged.  One campaign asked for 1,505 pages under a 150-page cap, was
    # scaled to 150 with a log line, and showed the user a plan it would never
    # fetch 90% of.
    scaled_down = budget["requested"] > budget["granted"]
    notice: dict[str, Any] = {
        "page_budget": {**budget, "scaled_down": scaled_down},
        "message": None,
    }
    if scaled_down:
        notice["message"] = (
            f"The plan asked for {budget['requested']:,} pages under a "
            f"{budget['max_pages']:,}-page cap; {budget['granted']:,} were granted (FR-186). "
            "Raise the campaign's max_pages to collect the rest."
        )
        log.info(
            "Plan for %s asked for %s pages under a %s-page cap; scaled to %s (FR-186)",
            campaign_id, budget["requested"], budget["max_pages"], budget["granted"],
        )

    item_ids = persist_plan(campaign_id, planned)
    repo.update_campaign(
        campaign_id,
        {
            "caps": caps,
            "status": "planned" if planned else campaign.get("status", "draft"),
            "stage": "planning",
            "plan_notice": notice,
        },
    )
    repo.record_audit(
        "campaign.plan_generated",
        job_seeker_id=job_seeker_id,
        entity_type="campaign",
        entity_id=campaign_id,
        detail={
            "sources": len(planned),
            "targets": found.target_count,
            "llm_used": llm_used,
            "degraded": degraded_reason,
            "page_budget": notice["page_budget"],
        },
    )

    reuse_assessment: knowledge_base.ReuseReport | None = None
    if assess_knowledge_base and planned:
        reuse_assessment = knowledge_base.assess_reuse(
            campaign_id,
            countries=countries,
            max_reuse_age_days=max_reuse_age_days,
        )

    summary = plan_summary(campaign_id, job_seeker_id)
    summary.update(
        {
            "plan_item_ids": item_ids,
            "rejected_sources": selection.rejected,
            "declined_sources": declines.blockers(declined_in_scope),
            "countries": countries,
            "keywords": keywords,
            "companies_in_scope": len(companies),
            "discovery": found.stats,
            "page_budget": budget,
            "plan_notice": notice,
            "llm_used": llm_used,
            "degraded_reason": degraded_reason,
            # FR-342: how much was reused and how many sources the reuse-age
            # cap re-opened, so the review screen and the run report can say
            # "12 reused, 3 refreshed" instead of only "done".
            "reuse": (
                {
                    "max_reuse_age_days": max_reuse_age_days,
                    "reused": reuse_assessment.reused_total,
                    "refreshed": reuse_assessment.refreshed,
                }
                if reuse_assessment is not None
                else None
            ),
        }
    )
    return summary


# Source types that can be searched with nothing but a keyword and a place, and
# source types that first need something to point at - a board slug, a company
# website, a legal identifier, a calendar.  A company-scoped source with no
# company is not a source with an empty query: it is a source with no work.
_KEYWORD_SOURCE_TYPES = frozenset({"job_board", "directory", "compensation", "linkedin"})
_COMPANY_SCOPED_TYPES = frozenset({"ats", "website", "news", "events", "registry"})


# Every vocabulary a plan item has ever named a board in.
_BOARD_SLUG_KEYS = ("slug", "board_slugs", "slugs")


def one_target_each(entry: dict, native_query: dict) -> list[dict]:
    """Split a query that names several boards into one item per board (FR-162).

    A plan item is one unit of work, and an ATS item that named three boards was
    fetched as its first board only: ``slug_of()`` returns ``board_slugs[0]``,
    the other two were dropped without a word, and the item still reported
    success.  Discovery emits one item per board already; this covers the
    remaining route, where a model answered with a list for a source that could
    not plan itself.
    """
    if (entry.get("source_type") or "") != "ats":
        return [native_query]
    slugs: list[str] = []
    for key in _BOARD_SLUG_KEYS:
        value = native_query.get(key)
        if isinstance(value, str):
            slugs.append(value)
        elif isinstance(value, list):
            slugs.extend(v for v in value if isinstance(v, str))
    unique = list(dict.fromkeys(s.strip().strip("/") for s in slugs if s.strip()))
    if len(unique) <= 1:
        return [native_query]
    base = {k: v for k, v in native_query.items() if k not in _BOARD_SLUG_KEYS}
    return [{**base, "slug": slug} for slug in unique]


def _keyword_searchable(entry: dict) -> bool:
    source_type = entry.get("source_type") or ""
    if source_type in _COMPANY_SCOPED_TYPES:
        return False
    if source_type in _KEYWORD_SOURCE_TYPES:
        return True
    capabilities = entry.get("query_capabilities") or {}
    return bool(capabilities.get("keyword_search")) and not capabilities.get("company_lookup")


def _planned_source(
    entry: dict, native_query: dict, pages: int, rationale: str
) -> PlannedSource:
    """One persisted plan item, stamped with the stage that must execute it."""
    pages = max(1, int(pages or 1))
    seconds, cost = estimate(entry, pages)
    stage = repo.STAGE_BY_SOURCE_TYPE.get(entry.get("source_type") or "", repo.STAGE_DISCOVER)
    return PlannedSource(
        adapter_key=entry["adapter_key"],
        native_query=native_query,
        rationale=rationale,
        estimated_pages=pages,
        estimated_seconds=seconds,
        estimated_cost_eur=cost,
        # ``estimated_pages`` is the item's page budget and the field the user
        # edits on the review screen (FR-163); duplicating it into ``caps`` only
        # created a second, staler ceiling that silently won.
        caps={"planned_pages": pages, "records_per_page": _per_page(entry), "stage": stage},
    )


def _per_page(entry: dict) -> int:
    capabilities = entry.get("query_capabilities") or {}
    try:
        return max(1, min(100, int(capabilities.get("max_results_per_query"))))
    except (TypeError, ValueError):
        return knowledge_base.DEFAULT_RECORDS_PER_PAGE


def _public_composite(composite: dict, do_not_disclose: set[str]) -> dict:
    """CR-410/FR-106: strip do-not-disclose and special-category fields before egress."""
    payload = {
        key: from_json(composite.get(key), composite.get(key))
        for key in (
            "narrative", "career_trajectory", "core_competencies", "adjacent_competencies",
            "seniority", "domains", "achievements", "inferred_preferences", "constraints",
        )
        if composite.get(key)
    }
    return redact(payload, do_not_disclose)


def _public_dream(dream: dict, do_not_disclose: set[str] | None = None) -> dict:
    """CR-410: the dream job model leaves the machine too, so it is redacted too."""
    payload = {
        key: from_json(dream.get(key), dream.get(key))
        for key in (
            "statement", "target_roles", "role_families", "responsibilities",
            "company_characteristics", "culture_values", "deal_breakers",
        )
        if dream.get(key)
    }
    return redact(payload, do_not_disclose)


def persist_plan(campaign_id: str, planned: list[PlannedSource]) -> list[str]:
    """Replace the campaign's plan, preserving exclusions and provenance (FR-163, FR-166).

    A source that survives a re-plan keeps its plan-item row rather than being
    deleted and re-inserted: the provenance rows written by an earlier
    collection point at that id, and FR-166 asks for that link to hold.  A
    source the new plan drops is deleted only when nothing points at it; when
    something does, the row is retired as ``skipped`` so it stops being
    collected without breaking the records it already produced.
    """
    stored = repo.list_plan_items(campaign_id)
    #: Keyed for matching, last row of a key winning as it always has.  ``stored``
    #: is kept alongside it because this dict *loses* rows: a campaign planned
    #: before the collapse net below existed can hold twenty items under one key,
    #: and nineteen of them are invisible here.  Deciding what to retire from the
    #: survivors alone deleted those nineteen outright - one running campaign has
    #: 16,234 provenance rows pointing at 39 such items, and ``source_plan_item_id``
    #: carries no foreign key, so the link would not fail loudly, it would just be
    #: gone.  Retirement is decided over ``stored`` instead (FR-166).
    existing = {_plan_key(i["adapter_key"], i.get("native_query")): i for i in stored}
    referenced = repo.plan_items_with_provenance(campaign_id)
    ids: list[str] = []
    seen: set[tuple[str, str]] = set()
    collapsed: dict[str, int] = {}
    #: The label of the first item each adapter collapsed, so the warning names
    #: the *target* that was planned twice and not only the source it belongs to
    #: (FR-162).  "Dropped 19 duplicate plan item(s) for board.actiris" cannot be
    #: checked against anything; the label can.
    collapsed_first: dict[str, str] = {}
    for item in planned:
        key = _plan_key(item.adapter_key, item.native_query)
        if key in seen:
            # Two items asking the same source the same question are not two
            # units of work: collection would issue the identical request twice
            # and write the same records under two provenance parents.  Nothing
            # legitimately produces one - stage follows source_type, so every
            # item of an adapter carries the same stage - so this is a net, not
            # a policy, and it says so out loud rather than quietly halving a
            # plan someone will later count.
            collapsed[item.adapter_key] = collapsed.get(item.adapter_key, 0) + 1
            collapsed_first.setdefault(
                item.adapter_key, plan_item_label(item.adapter_key, item.native_query)[0]
            )
            continue
        seen.add(key)
        row = item.to_row()
        previous = existing.pop(key, None)
        if previous is None:
            ids.append(repo.insert_plan_item(campaign_id, row))
            continue
        row["excluded_by_user"] = 1 if previous.get("excluded_by_user") else 0
        row["status"] = "planned"
        row["last_error"] = None
        repo.update_plan_item(previous["id"], row)
        ids.append(previous["id"])

    for adapter_key, count in sorted(collapsed.items()):
        log.warning(
            "Dropped %d duplicate plan item(s) for %s (first: %s): same source, same "
            "query, so they would have fetched the same records twice (FR-162, FR-166)",
            count, adapter_key, collapsed_first.get(adapter_key, ""),
        )

    matched = set(ids)
    retired = [i["id"] for i in stored if i["id"] not in matched and i["id"] in referenced]
    for retired_id in retired:
        repo.update_plan_item(
            retired_id, {"status": "skipped", "last_error": "no longer part of the plan"}
        )
    repo.delete_plan_items(campaign_id, keep_ids=[*ids, *retired])
    return ids


# Volatile keys: they say how far into a source a run may go, never which part
# of it this item reads.  ``page`` is the clearest case - a stored plan item
# never owns its page, because ``collection._run_page`` re-issues the item once
# per page with its own counter in ``native_query["page"]`` (see
# ``vacancy_source.requested_page``) - and the rest are ceilings on the same
# walk.  Only a key of that kind belongs here.  A key that names *what* is
# fetched must not: since ``persist_plan`` began collapsing items that share an
# identity, an over-broad entry no longer merely mismatches a row on a re-plan,
# it deletes work.  ``countries`` sat here and did exactly that.
# ``compensation.eurostat_ses`` plans one item per twelve-country chunk of a
# survey it can only request twelve countries at a time from, and ``countries``
# is the only key those items differ in, so a nineteen-country campaign had its
# second chunk dropped as a duplicate and never learned a salary band for the
# seven countries in it.  The same collision gave both chunks one ``target_key``
# (N4), so per-target reuse called a chunk fetched that no run had fetched.
_PLAN_KEY_IGNORED = frozenset({"page", "pages", "max_records", "max_pages"})


def _plan_key(adapter_key: str, native_query: Any) -> tuple[str, str]:
    """The identity of a unit of work, for matching a re-plan to its rows (FR-166).

    Keyed on what the item *fetches*, not on the adapter alone.  One item per
    adapter made ``(adapter_key, strategy)`` unique by accident; the moment an
    ATS source plans one item per company, that key made every company collide,
    and a provenance row came back describing a different employer.
    """
    payload = native_query if isinstance(native_query, dict) else {}
    identity = {
        k: v for k, v in sorted(payload.items()) if k not in _PLAN_KEY_IGNORED and v not in (None, "", [], {})
    }
    if not identity:
        return adapter_key, ""
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return adapter_key, digest


ATS_ADAPTER_PREFIX = "ats."


def target_key(adapter_key: str, native_query: Any, board: tuple[str, str] | None = None) -> str:
    """A stable name for the thing one plan item reads (N4, FR-342).

    This is the key the fetch ledger is written under, and therefore the key
    anything asking "was *this* target read recently" must ask with.  It lives
    here, next to :func:`_plan_key` it is built on, because two callers need the
    identical answer and had grown separate ones: collection wrote ledger rows
    under the board slug while the reuse assessment counted rows per adapter, so
    the two never met and one fresh board stood for every board of its vendor.

    ``board`` is the ``(vendor, slug)`` an already-loaded adapter resolved, which
    is authoritative.  Without one - the reuse assessment loads no adapters - an
    ATS item is named from its own key and slug, which agrees with it because
    every ATS adapter key is ``ats.<vendor>``.
    """
    if board is None:
        payload = native_query if isinstance(native_query, dict) else {}
        slug = str(payload.get("slug") or "").strip()
        if slug and adapter_key.startswith(ATS_ADAPTER_PREFIX):
            board = (adapter_key[len(ATS_ADAPTER_PREFIX):].lower(), slug)
    if board:
        return f"{board[0]}/{board[1]}"
    _, digest = _plan_key(adapter_key, native_query)
    return digest or adapter_key


# ---------------------------------------------------------------------------
# Telling one plan item from another (FR-162, FR-163)
# ---------------------------------------------------------------------------

#: Subdomains that say which door of a site was used, never which employer it
#: belongs to.  ``jobs.northwind.com`` and ``northwind.com`` are one target to a
#: reader and stripping them is what stops the list reading as two.
_LABEL_HOST_TRIM = re.compile(r"^(www|jobs|careers|m)\.", re.IGNORECASE)

#: The longest label a per-source row and a one-line activity feed can both
#: hold.  Measured over all 57,250 plan items: median 10, p95 27.
LABEL_MAX_CHARS = 48

#: The longest single term inside a label.  Without it one 90-character Workday
#: search string is the whole label and the company it belongs to is cut off.
_LABEL_TERM_CHARS = 28


def _label_list(payload: dict, *keys: str) -> list[str]:
    """Read one vocabulary more than the adapters' own readers know about.

    :func:`~dreamjob.adapters.vacancy_source.query_terms` and its siblings are
    the *collection* pipeline's readers: they answer for the keys an adapter
    fetches with, and widening them there would hand every adapter a key none
    of them writes.  A label is read off plans older passes wrote in their own
    words - ``topics``, ``regions`` - so those keys are asked for here instead.
    """
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        value = payload.get(key)
        candidates = value if isinstance(value, (list, tuple, set)) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate = candidate.get("label") or candidate.get("name")
            text = str(candidate or "").strip()
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
    return out


def _scalar_term(payload: dict) -> str:
    """A search string this item alone issues, as against the shared keyword bag.

    A list under ``keywords``/``queries`` is the campaign's scoring vocabulary
    and is byte-identical on every sibling; a *scalar* ``search_text``/``query``
    is the one query this item sends, and is the only thing telling Deloitte's
    seven Workday items apart.
    """
    for key in ("search_text", "query", "keyword"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _label_host(url: Any) -> str:
    """The site a crawl seed or a feed points at, without its scheme or door."""
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "https://" + text.lstrip("/")
    host = (urlparse(text).netloc or "").split("@")[-1].split(":")[0]
    return _LABEL_HOST_TRIM.sub("", host).lower()


def _label_slugs(payload: dict) -> list[str]:
    """The board slugs this item reads, in either the singular or plural key."""
    raw = payload.get("slug") or payload.get("board_slugs") or payload.get("slugs") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(slug).strip() for slug in raw if str(slug).strip()]


def _label_company(payload: dict) -> str:
    """The employer this item is about, in whichever vocabulary named it."""
    for key in ("company_name", "employer", "organisation"):
        if str(payload.get(key) or "").strip():
            return str(payload[key]).strip()
    company = payload.get("company")
    if isinstance(company, dict):
        return str(company.get("name") or company.get("legal_name") or "").strip()
    if isinstance(company, str):
        return company.strip()
    return ""


def _label_sections(payload: dict) -> list[str]:
    """NACE sections, which is what one partition of a register sweep asks for."""
    raw = (
        payload.get("nace_section")
        or payload.get("nace_sections")
        or payload.get("sector_codes")
        or payload.get("sectorCodes")
        or []
    )
    if isinstance(raw, (str, int)):
        raw = [raw]
    return [str(code).strip().upper() for code in raw if str(code).strip()]


def _label_regions(payload: dict) -> list[str]:
    """NUTS or region codes, which is the other axis a sweep is partitioned on."""
    for key in ("nuts_codes", "nuts", "region_codes", "regions", "region"):
        raw = payload.get(key)
        if isinstance(raw, str):
            raw = [raw]
        codes = [str(code).strip().upper() for code in (raw or []) if str(code).strip()]
        if codes:
            return codes
    return []


def _clip_label(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _terms_phrase(terms: list[str]) -> str:
    """The first term and how many others there are: "data engineer +4"."""
    if not terms:
        return ""
    head = _clip_label(terms[0], _LABEL_TERM_CHARS)
    return f"{head} +{len(terms) - 1}" if len(terms) > 1 else head


def plan_item_label(adapter_key: str, native_query: Any) -> tuple[str, str]:
    """A short name telling one plan item apart from its siblings (FR-162, FR-163).

    ``source_plan_item`` has no name column and ``display_name`` belongs to the
    *adapter*, so a plan with 2,417 Personio boards in it renders 2,417 rows
    reading "Personio".  What separates them is in ``native_query``, and every
    adapter family writes a different shape there.

    Returns ``(label, detail)``: ``label`` is the axis siblings differ on, at
    most 48 characters and never empty; ``detail`` is the shared query shape,
    for the muted second line.  Keyed on the *shape of the query* rather than on
    the adapter key, so an adapter this function has never seen still gets a
    name from whichever rule its query answers.
    """
    query = native_query if isinstance(native_query, dict) else {}
    terms = query_terms(query) or _label_list(query, "topics")
    detail_bits: list[str] = []
    label = ""

    # R1: the plan already named it.
    for key in ("label", "name", "title"):
        if str(query.get(key) or "").strip():
            label = str(query[key]).strip()
            break

    if not label:
        slugs = _label_slugs(query)
        company = _label_company(query)
        # R2/R3: a named target - a company's board, a register subject.  The
        # slug goes to the detail line as ``target_key`` spells it, so the row
        # and the fetch ledger name the same thing the same way (N4).
        if company or slugs:
            label = company or "/".join(slugs[:2])
            if slugs:
                vendor = (
                    adapter_key[len(ATS_ADAPTER_PREFIX):]
                    if adapter_key.startswith(ATS_ADAPTER_PREFIX)
                    else adapter_key.split(".")[-1]
                )
                more = f" +{len(slugs) - 1}" if len(slugs) > 1 else ""
                detail_bits.append(f"{vendor}/{slugs[0]}{more}")
            entity = query.get("company") if isinstance(query.get("company"), dict) else {}
            legal = str(entity.get("legal_id") or entity.get("vat_number") or "").strip()
            if legal:
                detail_bits.append(legal)

    if not label:
        # R4: a URL is the target - a crawl seed, a newsroom feed.
        seeds = query.get("crawl_seeds") if isinstance(query.get("crawl_seeds"), list) else []
        url = (
            query.get("url")
            or query.get("feed_url")
            or query.get("website")
            or (seeds[0] if seeds else None)
        )
        host = _label_host(url)
        if host:
            label = host
            paths = query.get("paths") if isinstance(query.get("paths"), list) else []
            if paths:
                detail_bits.append(f"{len(paths)} paths")

    if label:
        # A named target's siblings differ on the one query they each send, and
        # on nothing else: Deloitte's seven Workday rows share their slug.
        term = _scalar_term(query)
        if term:
            label = f"{label} · {_terms_phrase([term])}"

    if not label:
        # R5: one partition of a register sweep - region x sector x period.
        sections = _label_sections(query)
        regions = _label_regions(query)
        if sections or (regions and adapter_key.startswith("board.")):
            where = regions[0] if regions else (country_terms(query) or ["?"])[0]
            label = f"{where} · NACE {', '.join(sections[:3])}" if sections else where
            period = str(
                query.get("publication_period") or query.get("publicationPeriod") or ""
            ).strip()
            if period:
                detail_bits.append(period.replace("_", " ").lower())

    if not label and terms:
        # R6: a keyword search, which is what a job board is asked.
        label = _terms_phrase(terms)
        places = (
            location_terms(query)
            or _label_list(query, "region", "regions")
            or country_terms(query)
        )
        if places:
            label += f" · {places[0]}"

    if not label and not adapter_key.startswith(ATS_ADAPTER_PREFIX):
        # R7: a country sweep.  Not for an ATS item: one with no slug and no
        # company names no board, and "BE" would say it reads the whole country.
        codes = country_terms(query)
        if codes:
            label = ", ".join(codes[:3])
            occupations = query.get("occupations") if isinstance(
                query.get("occupations"), list
            ) else []
            if occupations:
                detail_bits.append(f"{len(occupations)} occupations")

    # The page is an axis of its own: collection re-issues one plan item once
    # per page, so a stored ``page`` is which slice of the source this row is.
    page = requested_page(query)
    if page is not None:
        label = f"{label} · page {page}" if label else f"page {page}"
    language = str(query.get("language") or "").strip()
    if language and len(language) <= 5:
        detail_bits.append(language)

    label = _clip_label(label, LABEL_MAX_CHARS)

    if not label:
        # R8: nothing in the query names a target.  The fetch ledger's own key
        # is used, so a row nobody can name is still greppable in the ledger.
        key = target_key(adapter_key, query)
        label = f"#{key[:8]}" if key and key != adapter_key else adapter_key

    if len(terms) > 1 and "+" not in label:
        detail_bits.append(f"{len(terms)} keywords")
    return label, " · ".join(detail_bits[:3])


def short_source_name(display_name: str) -> str:
    """The head of a catalogue name, for a one-line feed (FR-361).

    ``Actiris (Brussels public employment service)`` is 44 characters before the
    item's own label starts.  The parenthetical is what the per-source row has
    room for and a log line does not.
    """
    return (display_name or "").split(" (")[0].strip() or display_name


def aggregate_by_adapter(items: list[dict], catalogue: dict[str, dict]) -> list[dict]:
    """One row per source, whatever the plan holds behind it (FR-163).

    A Campaign A plan is ~7,500 one-page ATS items and ~126 EURES partitions.
    Nobody reviews that item by item, and no screen should try to draw it: what
    a reader decides on is "6,900 boards, 33 minutes, no tokens" per source.
    The per-item rows are still there, one page at a time, for the reader who
    wants to look at a particular target.
    """
    by_key: dict[str, dict] = {}
    for item in items:
        key = item["adapter_key"]
        entry = catalogue.get(key, {})
        row = by_key.get(key)
        if row is None:
            row = by_key[key] = {
                "adapter_key": key,
                "display_name": entry.get("display_name") or key,
                "source_type": entry.get("source_type"),
                "access_method": entry.get("access_method"),
                "tos_status": entry.get("tos_status"),
                "targets": 0,
                "excluded": 0,
                "skipped": 0,
                "estimated_pages": 0,
                "estimated_seconds": 0,
                "estimated_cost_eur": 0.0,
                "statuses": {},
            }
        row["targets"] += 1
        status = item.get("status") or "planned"
        row["statuses"][status] = row["statuses"].get(status, 0) + 1
        if item["excluded_by_user"]:
            row["excluded"] += 1
            continue
        if status == "skipped":
            row["skipped"] += 1
            continue
        row["estimated_pages"] += int(item["estimated_pages"] or 0)
        row["estimated_seconds"] += int(item["estimated_seconds"] or 0)
        row["estimated_cost_eur"] += float(item["estimated_cost_eur"] or 0.0)
    for row in by_key.values():
        row["estimated_cost_eur"] = round(row["estimated_cost_eur"], 4)
    return sorted(by_key.values(), key=lambda r: (-r["estimated_pages"], r["adapter_key"]))


def plan_summary(
    campaign_id: str,
    job_seeker_id: str,
    *,
    offset: int = 0,
    limit: int | None = PLAN_PAGE_SIZE,
) -> dict:
    """The review screen's payload: sources, queries, volume, duration, cost (FR-163).

    ``items`` is one page of targets, not the whole plan: since discovery emits
    one item per board and per EURES partition, a plan is thousands of rows and
    returning all of them would put megabytes through the API for a screen that
    shows a table.  ``by_adapter`` carries the aggregate the screen actually
    reads, and ``totals`` counts the whole plan however few rows were returned.
    """
    campaign = repo.get_campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")
    items = repo.list_plan_items(campaign_id)
    active = [i for i in items if not i["excluded_by_user"] and i["status"] != "skipped"]
    catalogue = {c["adapter_key"]: c for c in repo.list_catalogue(enabled_only=False)}
    caps = campaign.get("caps") or DEFAULT_CAPS
    max_pages = int((caps if isinstance(caps, dict) else {}).get("max_pages") or 0)
    planned_pages = sum(int(i["estimated_pages"] or 0) for i in active)
    by_adapter = aggregate_by_adapter(items, catalogue)

    start = max(0, int(offset or 0))
    page = items[start : start + limit] if limit and limit > 0 else items[start:]
    for item in page:
        entry = catalogue.get(item["adapter_key"], {})
        item["display_name"] = entry.get("display_name") or item["adapter_key"]
        item["source_type"] = entry.get("source_type")
        item["access_method"] = entry.get("access_method")
        item["tos_status"] = entry.get("tos_status")
        # FR-162: the same three fields the live dashboard names a source by, so
        # the review screen and the collection screen cannot call one plan item
        # two different things.
        label, label_detail = plan_item_label(item["adapter_key"], item.get("native_query"))
        item["label"] = label
        item["label_detail"] = label_detail
        item["target_key"] = target_key(item["adapter_key"], item.get("native_query"))
    notice = campaign.get("plan_notice") if isinstance(campaign.get("plan_notice"), dict) else {}
    stored_reuse = campaign.get("reuse_report")
    reuse = stored_reuse if isinstance(stored_reuse, dict) else {}
    return {
        "campaign_id": campaign_id,
        "status": campaign.get("status"),
        "stage": campaign.get("stage"),
        "caps": campaign.get("caps") or DEFAULT_CAPS,
        # FR-186: the page budget the plan was measured against, and the
        # notice when the cap forced it to be scaled down.  Both live on the
        # campaign so the screen states them after the planning call returns.
        "page_budget": notice.get("page_budget"),
        "plan_notice": notice or None,
        # FR-182/IR-101: which sources are declined right now, so a plan screen
        # can say "these will not be planned until an administrator re-enables
        # them" instead of silently omitting them.
        "declined_sources": declines.blockers(declines.active_map()),
        "generated_at": utcnow(),
        "totals": {
            "sources": len(active),
            "adapters": len(by_adapter),
            "sources_excluded": sum(1 for i in items if i["excluded_by_user"]),
            "sources_skipped_by_reuse": sum(1 for i in items if i["status"] == "skipped"),
            # FR-342: what the last reuse assessment saved, and how many plan
            # items the reuse-age cap sent back out for a refresh.
            "records_reused": int(reuse.get("reused_records") or 0),
            "sources_refreshed_by_age": int(reuse.get("refreshed_items") or 0),
            "estimated_pages": planned_pages,
            "estimated_seconds": sum(int(i["estimated_seconds"] or 0) for i in active),
            "estimated_cost_eur": round(
                sum(float(i["estimated_cost_eur"] or 0.0) for i in active), 4
            ),
            # FR-186: the review screen states the plan's volume next to the cap
            # it has to fit inside, rather than printing the two side by side and
            # never comparing them.
            "max_pages": max_pages,
            "exceeds_max_pages": max(0, planned_pages - max_pages) if max_pages else 0,
        },
        "by_adapter": by_adapter,
        "reuse_report": campaign.get("reuse_report"),
        "items": page,
        "items_page": {
            "offset": start,
            "limit": limit,
            "returned": len(page),
            "total": len(items),
            "has_more": start + len(page) < len(items),
        },
    }
