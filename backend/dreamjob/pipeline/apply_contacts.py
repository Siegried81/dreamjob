"""Contacts at scale for the Apply Browser (FR-301, FR-303, FR-304, FR-305).

:mod:`dreamjob.pipeline.contacts` answers "who should I write to about *this*
opportunity" and is worth a site crawl, an LLM-shaped company structure and a
ranked list of eight candidates.  The Apply Browser asks a coarser question of
a thousand rows at once - *does this vacancy have anything to apply to at all?*
- and the expensive per-opportunity path cannot answer it inside a screen's
patience.  This module is that second question.  It reuses the first module's
vocabulary throughout: :class:`~dreamjob.pipeline.contacts.ContactCandidate`,
its ranking, its NFR-303 storage scope, :mod:`~dreamjob.pipeline.email_patterns`
for finding and inferring addresses and :mod:`~dreamjob.pipeline.email_validate`
for the verdict.  Nothing here re-implements them.

**The ladder (FR-301, FR-303), in the order the brief fixes:**

1. a contact the knowledge base already holds for the company (free, FR-341);
2. the vacancy's own ``application_target`` when it is an address - the
   employer's stated channel, free to read, and the strongest evidence there
   is (``email_source_method = 'vacancy'``);
3. an address published on the company's own website or press page
   (``website`` / ``press``);
4. pattern inference from the other addresses on that domain
   (``pattern_inference``);
5. a generic careers mailbox, and only then (``pattern_inference``, with
   ``is_generic_mailbox`` set).

**The step the brief does not name, and why it is here.**  Every one of steps
3 to 5 needs a *domain*, and when this module was written 1,151 of the 1,202
companies that had posted a vacancy had none - the fifty-one that did were the
end-to-end fixtures.  EURES and Arbeitnow publish an employer name and nothing
else, and the ``employer.website`` field of the EURES payload is null for all
4,692 postings we hold.  So a domain has to be resolved before the ladder can
run at all, and how that is done decides whether the addresses below it are
evidence or fiction:

* ``company``  - already on the company record.  Trusted.
* ``vacancy_text`` - the domain of an address the employer itself published in
  the posting.  Trusted; the employer wrote it.
* ``derived_confirmed`` - a domain spelled from the company name and then
  **confirmed**: it must resolve to a mail exchanger (FR-304), its home page
  must answer, and that page must name the company.  A candidate that only has
  MX is discarded.  This matters: on 300 companies, name-plus-MX alone accepts
  ``house.com`` for "HOUSE OF RECRUITMENT SOLUTIONS BV" and ``federale.be`` for
  a federal ministry - addresses that would have gone to a real person at an
  unrelated company.  Requiring the page to name the company is what makes the
  derivation evidence rather than a guess (CR-405).

**Never invent an address.**  A company whose ladder produces nothing is
written to ``apply_contact_resolution`` with ``status = 'unreachable'`` and the
reason, and the Apply Browser shows it as such.  That row is a finding: it is
what stops the interface from offering a "generate" button for a company nobody
can reach, and it is what stops the next pass from re-crawling it.

**FR-305 rate limiting and caching.**  Three caches, each keyed on the thing
that is expensive:

* ``apply_domain_probe`` - the confirm/reject verdict per domain, so a domain
  is derived and fetched once, not once per company that spells to it;
* ``email_domain_state`` - the MX answer per domain, through
  :func:`email_validate.mx_for`, which the validation slice already maintains;
* ``http_cache`` and ``robots_cache`` - pages and rules, through the egress
  client, which also enforces robots.txt and the per-domain rate limit
  (FR-182, CR-402).  Nothing here fetches over HTTP by any other route.

**FR-306 / RK-08.**  Every stored address goes through
``ContactCandidate.as_contact_row`` and ``repositories.contacts.minimise``, so
the columns are the FR-306 minimum and nothing else about a person is kept.
NFR-302 objections are honoured because the ladder reads ``usable_contact`` and
because ``upsert_contact`` refuses to refresh a blocked row.

The bulk pass runs FR-304 with ``allow_smtp=False`` by default: an SMTP probe
per address would be several thousand connections to other people's mail
servers for one screen, which is neither what FR-305 has in mind nor polite
(CR-402).  Offline verdicts are therefore ``risky`` or ``unknown``, never
``valid`` - which is the honest answer - and the per-opportunity path in
:mod:`dreamjob.pipeline.contacts` still probes when a job seeker is actually
about to write to somebody.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.adapters.website import crawler
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import apply as repo
from dreamjob.db.repositories import company_domains as domain_repo
from dreamjob.db.repositories import contacts as contacts_repo
from dreamjob.db.repositories import registry_identity as identity_repo
from dreamjob.egress.client import EgressClient, RobotsDisallowed
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import contact_backup
from dreamjob.pipeline import contacts as discovery
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline import email_validate as validation
from dreamjob.pipeline.employer_registry_rung import page_identity

log = logging.getLogger(__name__)

# --- where a domain came from, stored verbatim in the resolution row -------
SOURCE_COMPANY = "company"
SOURCE_VACANCY_TEXT = "vacancy_text"
SOURCE_DERIVED = "derived_confirmed"
#: A domain the backup stage recovered from structured board evidence or the
#: posting's own URL fields, after the identity gate confirmed nothing.  Kept
#: apart from ``derived_confirmed`` because nobody judged its identity.
SOURCE_BACKUP = "backup_recovered"

# --- FR-305 cache windows --------------------------------------------------
#: How long a confirm/reject verdict for a domain stands before it is re-probed.
DOMAIN_PROBE_MAX_AGE_DAYS = 30
#: How long a company's reachability verdict stands before the ladder re-runs.
RESOLUTION_MAX_AGE_DAYS = 14
#: A domain that simply did not answer is retried sooner than one that was
#: judged: "no answer" is usually the network, not the company.
UNREACHABLE_RETRY_DAYS = 3

#: How long one company's resolution may run before the pass gives up on it.
#: ``asyncio.as_completed`` waits for every task, so a site that accepted the
#: connection and then never answered froze the whole sweep: the live job sat at
#: 299/300 for over twenty minutes and every later contacts phase queued behind
#: it.  180 s is generous next to the egress client's own per-request budget and
#: turns the one hung site into one recorded outcome.
COMPANY_TIMEOUT_SECONDS = 180.0

#: How long the pass waits for cancelled company tasks to unwind before it
#: settles anyway.  Cancellation is deliverable at every await in the ladder, so
#: this only bites when a task is stuck below the event loop; the job must not
#: share its fate.
CANCELLATION_GRACE_SECONDS = 10.0

#: Legal forms and the noise around them.  Stripped before a domain is spelled
#: from a name, because no company registers ``acme-bv.be``.
LEGAL_FORMS = frozenset(
    {
        "nv", "sa", "bv", "bvba", "sprl", "srl", "vof", "comm", "va", "cv", "cvba", "cvoa",
        "esv", "vzw", "asbl", "ivzw", "aisbl", "gcv", "scs", "sca", "se", "eesv",
        "gmbh", "mbh", "ag", "kg", "kgaa", "ohg", "gbr", "ug", "eg", "ev",
        "sas", "sarl", "eurl", "sasu", "sci", "scop", "snc",
        "ltd", "limited", "plc", "llp", "lp", "llc", "inc", "incorporated", "corp",
        "corporation", "co", "company", "holding", "holdings", "group", "groep", "groupe",
        "international", "belgium", "belgie", "belgique", "nederland", "deutschland",
        "france", "europe", "benelux",
        "aps", "ab", "oy", "spa", "srls", "as", "asa", "bhd", "pte", "pty", "sl", "sau",
    }
)

#: Words that carry no identity, so a domain is never spelled from them alone.
_STOPWORDS = frozenset({"the", "de", "het", "een", "and", "en", "et", "und", "of", "van", "der"})

#: Country-code top-level domains worth trying, per market.  The order is the
#: order they are tried, and ``.com`` is always last because it is the one a
#: parking service is most likely to already own.
TLDS_BY_COUNTRY: dict[str, tuple[str, ...]] = {
    "BE": ("be", "com", "eu"),
    "NL": ("nl", "com"),
    "LU": ("lu", "com"),
    "DE": ("de", "com"),
    "AT": ("at", "com"),
    "CH": ("ch", "com"),
    "FR": ("fr", "com"),
    "GB": ("co.uk", "com"),
    "UK": ("co.uk", "com"),
    "IE": ("ie", "com"),
    "ES": ("es", "com"),
    "IT": ("it", "com"),
    "PL": ("pl", "com"),
    "SE": ("se", "com"),
    "DK": ("dk", "com"),
    "NO": ("no", "com"),
    "FI": ("fi", "com"),
    "PT": ("pt", "com"),
    "US": ("com",),
    "CA": ("ca", "com"),
}
DEFAULT_TLDS: tuple[str, ...] = ("com",)

#: Job boards, aggregators and applicant-tracking hosts.  An address on one of
#: these is not the *employer's* domain, so it never becomes the domain the
#: rest of the ladder spells addresses on.  The vendor list covers every board
#: host :mod:`dreamjob.adapters.ats.detect` can name - including the rebrands a
#: tenant board redirects to (Recruitee's ``tellent.com``, Personio's
#: ``personio.com``) - because an address published on a board as boilerplate
#: is the platform's, not the employer's (FR-303).
#:
#: The live corpus added the rest: one host seen across hundreds of companies'
#: resolutions is boilerplate however plausible it looks.  ``arbeitnow.fr``
#: carried 497 resolutions (the French mirror of a board already listed),
#: ``greenhouse.com`` 32 and ``employinc.com`` 21 (the vendors themselves),
#: ``bruxellesformation.be`` 70 and ``actiris.brussels`` 5 (public employment
#: services printing their own address in every posting), ``wikipedia.org`` 10,
#: ``team.blue`` 9 (a hosting provider), ``esempio.com`` 5 (an ``example.com``
#: equivalent) and ``feather-insurance.com`` 49 (one employer's benefit scheme
#: reproduced in other employers' postings).
AGGREGATOR_DOMAINS = frozenset(
    {
        "europa.eu", "ec.europa.eu", "eures.europa.eu", "arbeitnow.com", "arbeitnow.co.uk",
        "arbeitnow.fr",
        "greenhouse.io", "greenhouse.com", "lever.co", "workday.com", "myworkdayjobs.com",
        "smartrecruiters.com",
        "personio.de", "personio.com", "recruitee.com", "recruitee-cdn.com", "tellent.com",
        "teamtailor.com", "teamtailor-cdn.com", "jobs.lever.co", "bamboohr.com",
        "workable.com", "careers-page.com", "ashbyhq.com", "jobvite.com", "icims.com",
        "successfactors.com", "sapsf.com", "sapsf.eu", "taleo.net", "softgarden.io",
        "join.com", "homerun.co", "employinc.com",
        "linkedin.com", "indeed.com", "monster.com", "stepstone.de", "vdab.be", "forem.be",
        "actiris.be", "actiris.brussels", "bruxellesformation.be", "jobat.be",
        "stepstone.be", "ictjob.be", "glassdoor.com", "glassdoor.es", "xing.com",
        "example.com", "example.org", "esempio.com", "domain.com", "email.com",
        "sentry.io", "wixpress.com", "wikipedia.org", "team.blue", "feather-insurance.com",
        "businesswire.com", "website-files.com",
        # The public JSON job feeds: their posting URLs are the attribution the
        # feeds' terms require, never the employer's site.
        "remotive.com", "remoteok.com", "jobicy.com", "himalayas.app", "adzuna.com",
        "adzuna.be", "adzuna.nl", "adzuna.fr", "adzuna.de", "adzuna.co.uk", "ycombinator.com",
    }
)

#: Domains an extraction once spelled out of prose and that must never become
#: an employer's domain again.  Each is a substring of an ordinary English
#: sentence - "statement at any point in time" ends in ``any.in``, "coordination
#: point for" hides ``ion.for``, "creative point of view" hides ``ive.of`` -
#: matched by the pre-fix obfuscated-address pattern.  The pattern no longer
#: produces them, and this set means a value already stored in the corpus
#: cannot be adopted again either.
JUNK_DOMAINS = frozenset(
    {"any.in", "ive.er", "ion.for", "ional.of", "ion.and", "ive.of"}
)

#: Free mailbox providers.  A small employer really does apply from one, so the
#: address is kept - but the *domain* is never used to spell other addresses.
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "hotmail.com", "hotmail.be", "hotmail.fr", "hotmail.de",
        "outlook.com", "outlook.be", "live.com", "live.be", "yahoo.com", "yahoo.fr", "yahoo.de",
        "icloud.com", "me.com", "aol.com", "gmx.de", "gmx.net", "web.de", "proton.me",
        "protonmail.com", "telenet.be", "skynet.be", "scarlet.be", "ziggo.nl", "kpnmail.nl",
        "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "t-online.de",
    }
)

#: Text that means the page is a parking or for-sale placeholder, not a company.
PARKING_MARKERS = (
    "domain is for sale", "buy this domain", "this domain is parked", "domain parking",
    "deze domeinnaam", "te koop", "diese domain", "ce domaine", "sedoparking",
    "godaddy.com/forsale", "hugedomains", "afternic", "dan.com", "under construction",
    "coming soon", "site not found", "default web page", "apache2 ubuntu default",
)

#: Below this many characters of extracted text a page carries no evidence.
MIN_PAGE_TEXT = 180

#: A company whose whole name is one word shorter than this gets no derived
#: domain.  ``fsc.de`` and ``alan.fr`` are somebody's domain; whose, a
#: three-letter name cannot establish.
MIN_SINGLE_TOKEN = 5

_WORD_RE = re.compile(r"[a-z0-9]+")
_URLISH_RE = re.compile(r"(?:https?://|www\.)\S+|\S+@\S+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Spelling a domain from a company name
# ---------------------------------------------------------------------------


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def name_tokens(name: str | None) -> list[str]:
    """The identifying words of a company name, legal form removed.

    ``"NOEL FRANKLIN BV"`` becomes ``["noel", "franklin"]``; ``"Rügamer &
    Steiner Consulting GmbH"`` becomes ``["rugamer", "steiner", "consulting"]``.
    A name that is nothing but a legal form has no tokens and therefore no
    derived domain, which is the correct answer.

    One-letter words are **kept**.  Dropping them read "Q Energy" as
    ``["energy"]`` and spelled ``energy.de``, whose home page naturally
    contains the word "energy" and therefore confirmed - an address at an
    unrelated company, which is the one outcome this module must not produce.
    ``qenergy.de`` is the honest candidate and simply fails to confirm when it
    is wrong.
    """
    lowered = strip_accents((name or "").lower())
    words = _WORD_RE.findall(lowered)
    kept = [w for w in words if w not in LEGAL_FORMS and w not in _STOPWORDS]
    # A name made entirely of legal forms ("Holding Group NV") keeps its words
    # rather than becoming nothing; there is no other identity to use.
    return kept or [w for w in words if w not in _STOPWORDS]


def looks_like_domain(name: str | None) -> str:
    """``"Taxtalente.de"`` is a company name that is already a domain."""
    candidate = strip_accents((name or "").strip().lower())
    if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,12}", candidate):
        return candidate
    return ""


def candidate_domains(name: str | None, country: str | None = None, limit: int = 5) -> list[str]:
    """Domains this company might plausibly own, best guess first.

    Two spellings only - the tokens run together and the tokens hyphenated -
    across the country's top-level domains.  The tempting third spelling, the
    first word on its own, is deliberately absent: it is what turns "ABSOLUTE
    @WORK BV" into ``absolute.com`` and "Autonoom Gemeentebedrijf Stedelijk
    Onderwijs Antwerpen" into ``autonoom.com``.  Every candidate still has to
    survive :func:`confirm_domain` before anything is spelled on it.
    """
    tokens = name_tokens(name)
    if not tokens:
        return []
    out: list[str] = []
    already = looks_like_domain(name)
    if already:
        out.append(already)
    # "Fsc" spells fsc.de, and a page about forest certification confirms it.
    # A single short word is a word, not an identity (CR-405).
    if len(tokens) == 1 and len(tokens[0]) < MIN_SINGLE_TOKEN:
        return out

    joined = "".join(tokens)
    hyphenated = "-".join(tokens)
    bases = [b for b in (joined, hyphenated) if 3 <= len(b) <= 40]
    if joined == hyphenated:
        bases = bases[:1]
    for base in bases:
        for tld in TLDS_BY_COUNTRY.get((country or "").upper(), DEFAULT_TLDS):
            out.append(f"{base}.{tld}")

    seen: list[str] = []
    for domain in out:
        if domain not in seen and domain not in AGGREGATOR_DOMAINS:
            seen.append(domain)
    return seen[:limit]


def usable_employer_domain(domain: str | None) -> str:
    """The domain, unless it belongs to a board, a vendor or a free mailbox provider."""
    value = (domain or "").strip().lower().lstrip(".")
    if not value or "." not in value:
        return ""
    if value in AGGREGATOR_DOMAINS or value in FREEMAIL_DOMAINS or value in JUNK_DOMAINS:
        return ""
    registrable = crawler.registrable_domain(value)
    if (
        registrable in AGGREGATOR_DOMAINS
        or registrable in FREEMAIL_DOMAINS
        or registrable in JUNK_DOMAINS
    ):
        return ""
    return value


# ---------------------------------------------------------------------------
# Confirming a derived domain (the CR-405 gate)
# ---------------------------------------------------------------------------


@dataclass
class DomainVerdict:
    """One domain, judged once and remembered (FR-305)."""

    domain: str
    outcome: str  # confirmed | rejected | no_mx | unreachable
    evidence: str = ""
    from_cache: bool = False

    @property
    def confirmed(self) -> bool:
        return self.outcome == "confirmed"


def company_named_on_page(
    name: str,
    html: str,
    *,
    country: str = "",
    domain: str = "",
    final_url: str = "",
) -> tuple[bool, str]:
    """Does this page belong to this company?  The whole derivation rests here.

    The rules live in :func:`employer_registry_rung.page_identity`, because the
    same question decides whether an address may be spelled on a domain and
    whether a page may be read as evidence of what an organisation does, and
    the two must not drift apart.  What this version added, after three
    namesakes were found confirmed in the corpus (``brightplus.com``,
    ``adequat.com``, ``think-about-it.com``): words common to thousands of
    company names do not count towards identity, a name with one identifying
    word must be what the page *calls itself* rather than words in its copy, a
    title segment that is only the domain is circular, the market has to appear
    on an off-market top-level domain, and the final URL after redirects is the
    domain the verdict attaches to.

    ``country``, ``domain`` and ``final_url`` are optional so that every
    existing caller keeps working; supplying them is what turns rules 3 and 5
    on, and :func:`confirm_domain` supplies all three.
    """
    decision = page_identity(
        name, html, domain=domain, country=country, final_url=final_url
    )
    return decision.confirmed, decision.reason


def _probe_is_fresh(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    age = _age_days(row.get("last_probed_at"))
    window = UNREACHABLE_RETRY_DAYS if row.get("outcome") == "unreachable" else (
        DOMAIN_PROBE_MAX_AGE_DAYS
    )
    return age < window


def _age_days(timestamp: str | None) -> float:
    if not timestamp:
        return 10**6
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return 10**6
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() / 86400


async def confirm_domain(
    domain: str,
    company_name: str,
    *,
    company_id: str | None = None,
    country: str = "",
    egress: EgressClient | None = None,
    force: bool = False,
) -> DomainVerdict:
    """Judge one candidate domain, cheapest check first (FR-304, FR-305, CR-402).

    MX before HTTP, deliberately: a domain that cannot receive mail is useless
    to this module whatever its home page says, and answering that from DNS
    costs one UDP packet instead of a robots.txt fetch and a page fetch on
    somebody else's server.
    """
    domain = usable_employer_domain(domain)
    if not domain:
        return DomainVerdict("", "rejected", "not an employer domain")

    cached = await asyncio.to_thread(repo.domain_probe, domain)
    if cached and not force and _probe_is_fresh(cached):
        return DomainVerdict(
            domain, cached["outcome"], cached.get("evidence") or "", from_cache=True
        )

    mx = await asyncio.to_thread(validation.mx_for, domain)
    if not mx.has_mx:
        verdict = DomainVerdict(domain, "no_mx", mx.error or "no mail exchanger")
        await asyncio.to_thread(
            repo.record_domain_probe, domain, verdict.outcome,
            company_id=company_id, evidence=verdict.evidence,
        )
        return verdict

    try:
        if egress is None:
            async with EgressClient(store_raw=False) as client:
                page = await client.fetch(f"https://{domain}")
        else:
            page = await egress.fetch(f"https://{domain}")
    except RobotsDisallowed as exc:
        # CR-402: not permission to read, so not evidence.  Never worked around.
        verdict = DomainVerdict(domain, "unreachable", f"robots.txt: {exc}"[:200])
    except Exception as exc:  # noqa: BLE001 - one dead site must not stop the pass
        verdict = DomainVerdict(domain, "unreachable", f"{type(exc).__name__}: {exc}"[:200])
    else:
        if not page.ok:
            verdict = DomainVerdict(domain, "unreachable", f"HTTP {page.status_code}")
        else:
            named, why = company_named_on_page(
                company_name,
                page.text,
                country=country,
                domain=domain,
                final_url=getattr(page, "url", "") or "",
            )
            verdict = DomainVerdict(domain, "confirmed" if named else "rejected", why)

    await asyncio.to_thread(
        repo.record_domain_probe, domain, verdict.outcome,
        company_id=company_id, evidence=verdict.evidence,
    )
    return verdict


# ---------------------------------------------------------------------------
# The ladder for one company
# ---------------------------------------------------------------------------


@dataclass
class CompanyOutcome:
    """What the ladder concluded about one company, for the report and the row."""

    company_id: str
    company_name: str
    vacancy_count: int
    status: str = "unreachable"
    domain: str | None = None
    domain_source: str | None = None
    email: str | None = None
    method: str | None = None
    validation: str | None = None
    is_generic: bool = False
    contact_id: str | None = None
    reason: str = ""
    reused: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        return self.status == "reachable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "vacancy_count": self.vacancy_count,
            "status": self.status,
            "domain": self.domain,
            "domain_source": self.domain_source,
            "email": self.email,
            "email_source_method": self.method,
            "email_validation": self.validation,
            "is_generic_mailbox": self.is_generic,
            "contact_id": self.contact_id,
            "reason": self.reason,
            "reused": self.reused,
            "notes": self.notes,
        }


def addresses_in_vacancies(hints: list[dict[str, Any]]) -> list[patterns.FoundAddress]:
    """Step 2 of the ladder: the employer's own stated application channel.

    ``application_target`` is read first because it is the field the employer
    filled in; the description is read second because small employers write
    "stuur je cv naar ..." in the body instead.  Both are free - the text is
    already in the database - which is why this runs before anything is
    fetched over the network.
    """
    found: dict[str, patterns.FoundAddress] = {}
    for hint in hints:
        source_url = hint.get("source_url") or ""
        target = (hint.get("application_target") or "").strip()
        if "@" in target and not target.lower().startswith(("http://", "https://")):
            for item in patterns.extract_addresses(
                target, source_url=source_url, method=patterns.METHOD_VACANCY
            ):
                found.setdefault(item.email, item)
        body = hint.get("description") or ""
        if "@" in body:
            for item in patterns.extract_addresses(
                body, source_url=source_url, method=patterns.METHOD_VACANCY
            ):
                # Boilerplate on an aggregator page is not the employer's inbox.
                if usable_employer_domain(item.domain) or item.domain in FREEMAIL_DOMAINS:
                    found.setdefault(item.email, item)
    return list(found.values())


async def resolve_domain(
    company: dict[str, Any],
    hints: list[dict[str, Any]],
    published: list[patterns.FoundAddress],
    *,
    egress: EgressClient | None = None,
    derive: bool = True,
) -> tuple[str, str, str]:
    """``(domain, source, note)`` for one company - the ladder's prerequisite."""
    stated = usable_employer_domain(
        patterns.domain_of(company.get("company_domain") or company.get("careers_url"))
    )
    if stated:
        return stated, SOURCE_COMPANY, "the company record already carries a domain"

    # Everything below rung 1 - including the domain of an address the employer
    # published in its own posting - is the domain ladder's question, and it is
    # asked there rather than here.  This rung used to take that domain on
    # sight, and on this corpus that wrote ``jobs@just.fgov.be`` for the
    # National Institute for Criminalistics: the posting carries a
    # ``@just.fgov.be`` address, the shortcut read the domain off it, and the
    # careers mailbox was then *spelled* on a domain the gate had already taken
    # back from that company as a namesake.  ``held_domains`` asks the one
    # question that separates "the employer's own domain" from "a domain the
    # posting happens to mention" - does the label carry a word of the company's
    # name - and hands everything else to the gate; and only that path checks
    # ``company_domain_revocation`` (CR-405).
    return await derived_domain(company, egress=egress, derive=derive)


async def derived_domain(
    company: dict[str, Any], *, egress: EgressClient | None = None, derive: bool = True
) -> tuple[str, str, str]:
    """Rungs 2 to 5 of the domain question, asked of the module that owns it.

    This module used to spell its own candidates and judge them with
    :func:`confirm_domain`.  It no longer does, and the reason is a safety one
    rather than a tidiness one.  :mod:`dreamjob.pipeline.company_domains` grew
    four rules out of live evidence that :func:`confirm_domain` does not have
    and cannot get, because they are not about the *page* - which is all
    :func:`employer_registry_rung.page_identity` sees - but about what kind of
    evidence a *spelling* is:

    * a one-identifying-word name must sit on the market's own top-level
      domain, which is what ``adequat.eu`` failed;
    * a spelled candidate whose page carries less than
      :data:`MIN_PAGE_TEXT` characters is not evidence, which is what
      ``konverthr.com`` failed;
    * a base shorter than five characters is an acronym, not a name, which is
      what ``prs.com`` failed - and which :func:`candidate_domains` would still
      offer, because its floor applies to a lone *token* rather than to the
      spelled base;
    * a domain that the gate took back from this company stays taken back
      (``company_domain_revocation``).

    Running the weaker gate here after the stronger one has already refused a
    company would put those four namesakes back, one address at a time.  So
    the ladder reads that module's verdict, and only asks it to walk a company
    it has never judged.  Reused, not re-implemented (CR-405).
    """
    from dreamjob.pipeline import company_domains as domains  # local: it imports this module

    company_id = str(company.get("company_id") or "")
    verdict = await asyncio.to_thread(domain_repo.resolution_for, company_id)
    if verdict is None and not derive:
        return "", "", (
            "no domain on the company record, and the domain ladder has not judged "
            "this company"
        )
    if verdict is not None and _age_days(verdict.get("resolved_at")) < (
        domains.RESOLUTION_MAX_AGE_DAYS
    ):
        stored = usable_employer_domain(verdict.get("domain"))
        if verdict.get("status") == "resolved" and stored:
            return stored, str(verdict.get("source") or SOURCE_DERIVED), str(
                verdict.get("evidence") or "the domain ladder confirmed this domain"
            )
        return "", "", (
            "the domain ladder judged this company and confirmed nothing: "
            f"{verdict.get('reason') or 'no candidate survived the identity gate'}"
        )

    revoked = {
        (str(row.get("company_id") or ""), str(row.get("domain") or "").lower())
        for row in await asyncio.to_thread(identity_repo.revocations, 2000)
    }
    # ``use_register`` is off here on purpose.  The register rung is one host
    # with one rate limit and it belongs to the pass that owns it; a contacts
    # pass running eight companies wide has no business opening a second front
    # on it, and on this corpus it published a web address for one company in
    # thirty-eight (CR-402, FR-305).
    async def walk(client: EgressClient) -> Any:
        return await domains.resolve_company(
            company, egress=client, use_register=False, revoked=revoked
        )

    if egress is None:
        async with EgressClient(store_raw=False) as client:
            outcome = await walk(client)
    else:
        outcome = await walk(egress)
    await asyncio.to_thread(domains.persist, outcome)
    if outcome.resolved and outcome.domain:
        return outcome.domain, str(outcome.source or SOURCE_DERIVED), outcome.evidence
    return "", "", outcome.reason


async def gather_candidates(
    company: dict[str, Any],
    domain: str,
    published: list[patterns.FoundAddress],
    *,
    egress: EgressClient | None = None,
    crawl_site: bool = True,
    allow_generic: bool = True,
    diagnostics: dict[str, Any] | None = None,
) -> list[discovery.ContactCandidate]:
    """Steps 1 and 3 to 5 of the FR-301 ladder, as ranked candidates."""
    company_id = company.get("company_id") or ""
    candidates: list[discovery.ContactCandidate] = []

    # 1 - what the knowledge base already holds (FR-341 reuse).
    candidates.extend(discovery.candidates_from_stored(company_id, None))

    # 2 - the addresses the employer published in its own posting.
    for address in published:
        role = validation.is_role_address(address.email)
        candidates.append(
            discovery.ContactCandidate(
                full_name=None,
                role_title="Application address in the vacancy",
                tier=discovery.TIER_GENERIC if role else discovery.TIER_HR,
                source=address.source_url or patterns.METHOD_VACANCY,
                email=address.email,
                email_source_method=patterns.METHOD_VACANCY,
                is_generic_mailbox=role,
                confidence=address.confidence,
                rationale="The employer's own stated application channel (FR-303 vacancy)",
            )
        )

    if not domain:
        return discovery.rank(candidates)

    # 3 - addresses published on the company's own pages (FR-303 website/press).
    harvested: list[patterns.FoundAddress] = []
    if crawl_site:
        try:
            harvested = await patterns.collect_from_site(
                domain, careers_url=company.get("careers_url"), egress=egress, max_pages=12,
                diagnostics=diagnostics,
            )
        except Exception as exc:  # noqa: BLE001 - the site is optional evidence
            log.info("Could not read %s for addresses: %s", domain, exc)
    on_domain = [a for a in harvested if a.domain == domain]
    for address in on_domain:
        if validation.is_role_address(address.email):
            continue
        candidates.append(
            discovery.ContactCandidate(
                full_name=address.full_name,
                role_title=None,
                # ``classify_role`` matches the HR and management vocabulary in
                # the four languages of the market; the words around a published
                # address are the only role description this page offers.
                tier=discovery.classify_role(address.context),
                source=address.source_url or address.method,
                email=address.email,
                email_source_method=address.method,
                confidence=address.confidence,
                rationale="Published on the company's own pages (FR-303 website)",
            )
        )

    # 3b - the search index, when the site blocked us or published nothing new.
    # A search engine has already crawled the pages an F5/Cloudflare challenge
    # hides from us, so its results are the only way into those sites (FR-303).
    if crawl_site and patterns.search_enabled():
        try:
            found = await patterns.search_for_contacts(
                company.get("company_name") or "", domain, egress=egress
            )
        except patterns.SearchDisabled:
            found = []
        except Exception as exc:  # noqa: BLE001 - search is an optional source
            log.info("Search source failed for %s: %s", domain, exc)
            found = []
        for address in found:
            candidates.append(
                discovery.ContactCandidate(
                    full_name=address.full_name,
                    role_title=None,
                    tier=discovery.classify_role(address.context),
                    source=address.source_url or patterns.METHOD_SEARCH,
                    email=address.email,
                    email_source_method=patterns.METHOD_SEARCH,
                    is_generic_mailbox=validation.is_role_address(address.email),
                    confidence=address.confidence,
                    rationale="Found through the search index (FR-303 search)",
                )
            )

    # 4 - the domain's convention, learned from what was just seen (FR-303).
    observations = [
        (a.full_name, a.email)
        for a in on_domain
        if a.full_name and not validation.is_role_address(a.email)
    ]
    inference = await asyncio.to_thread(patterns.learn_domain_pattern, domain, observations)
    if inference.pattern:
        for person in _hiring_people(company):
            for guess in patterns.candidates_for_person(
                person["full_name"], domain, inference=inference, limit=2
            ):
                candidates.append(
                    discovery.ContactCandidate(
                        full_name=person["full_name"],
                        role_title=person.get("role_title"),
                        tier=discovery.classify_role(person.get("role_title")),
                        source=f"pattern {inference.pattern} on {domain}",
                        email=guess.email,
                        email_source_method=patterns.METHOD_PATTERN,
                        confidence=guess.confidence,
                        rationale=(
                            f"Inferred from the {inference.pattern!r} convention this domain "
                            f"uses in {inference.supporting} of {inference.sample_count} "
                            "observed addresses (FR-303 pattern inference)"
                        ),
                    )
                )

    # 5 - the generic careers mailbox, last (FR-301 tier 3).
    if allow_generic:
        candidates.extend(
            discovery.generic_mailbox_candidates(domain, company.get("careers_url"), harvested)
        )

    return discovery.rank(candidates)


def _hiring_people(company: dict[str, Any]) -> list[dict[str, Any]]:
    """Named people at the company whose role reaches the hiring decision."""
    out = []
    for person in company.get("key_people") or []:
        if not isinstance(person, dict):
            continue
        full_name = (person.get("name") or person.get("full_name") or "").strip()
        role = person.get("role") or person.get("role_title") or person.get("title")
        if not full_name or " " not in full_name:
            continue
        tier = discovery.classify_role(role)
        if tier in (discovery.TIER_HIRING_MANAGER, discovery.TIER_HR):
            out.append({"full_name": full_name, "role_title": role})
    return out[:3]


def first_usable(
    candidates: list[discovery.ContactCandidate], *, allow_smtp: bool = False
) -> tuple[discovery.ContactCandidate, validation.ValidationResult] | None:
    """Validate down the ranked list and take the first FR-304 does not rule out.

    Ranked order is FR-301 order, so a published address is tested before an
    inferred one and the careers mailbox is tested last.  A ``valid`` verdict
    ends the search; anything short of it is kept only if nothing better turns
    up, and ``invalid`` is discarded outright (FR-304).

    NFR-302 is checked here rather than after the write.  ``contact``'s trigger
    does block a re-collected objecting address on insert, but by then the
    ladder has already called it the answer; asking ``contact_objection``
    first means an objection ends the candidate's life before it is validated,
    stored or reported as reachable.
    """
    fallback: tuple[discovery.ContactCandidate, validation.ValidationResult] | None = None
    for candidate in candidates:
        if not candidate.email or candidate.blocked:
            continue
        if discovery.is_blocked(candidate.email, candidate.linkedin_url):
            candidate.blocked = True
            log.info("Skipping %s: the address carries an objection (NFR-302)", candidate.email)
            continue
        verdict = validation.validate(candidate.email, allow_smtp=allow_smtp)
        candidate.validation = verdict.result
        candidate.validation_detail = verdict.detail
        if verdict.result == validation.VALID:
            return candidate, verdict
        if verdict.result == validation.INVALID:
            continue
        if fallback is None or validation.VERDICT_RANK.get(
            verdict.result, 0
        ) > validation.VERDICT_RANK.get(fallback[1].result, 0):
            fallback = (candidate, verdict)
    return fallback


def backup_candidates(
    findings: list[contact_backup.BackupFinding],
) -> list[discovery.ContactCandidate]:
    """FR-301 candidates for the backup findings, labels preserved.

    A published backup address (``stored_document``/``ats_board``) keeps its
    method and is therefore never marked uncertain; a composed generic from a
    recovered domain stays ``pattern_inference`` and stays uncertain.  The
    ranking, validation and storage steps then treat them like any other
    candidate (FR-301, FR-303, FR-304).
    """
    out: list[discovery.ContactCandidate] = []
    for finding in findings:
        address = finding.address
        role = validation.is_role_address(address.email)
        out.append(
            discovery.ContactCandidate(
                full_name=address.full_name,
                role_title="Careers mailbox" if role else None,
                tier=discovery.TIER_GENERIC if role else discovery.classify_role(address.context),
                source=address.source_url or address.method,
                email=address.email,
                email_source_method=address.method,
                is_generic_mailbox=role,
                confidence=address.confidence,
                rationale=finding.rationale,
            )
        )
    return out


async def _walk_backup(
    company: dict[str, Any],
    *,
    egress: EgressClient | None,
    crawl_site: bool,
) -> tuple[list[contact_backup.BackupFinding], str, str]:
    """The backup stage, never allowed to fail the ladder.

    The harvested findings already include the crawl and generics of a
    recovered domain (that is the module's own bounded work), so the caller
    appends them rather than fetching the same pages a second time.
    """
    try:
        return await contact_backup.harvest_backup_addresses(
            company, egress=egress, crawl_site=crawl_site
        )
    except Exception as exc:  # noqa: BLE001 - a backup source is optional evidence
        log.info("Backup contact discovery failed for %s: %s", company.get("company_id"), exc)
        return [], "", f"backup failed: {type(exc).__name__}"


async def resolve_company(
    company: dict[str, Any],
    *,
    egress: EgressClient | None = None,
    campaign_id: str | None = None,
    allow_smtp: bool = False,
    crawl_site: bool = True,
    derive_domains: bool = True,
    allow_generic: bool = True,
    backup: bool = False,
) -> CompanyOutcome:
    """Walk the whole FR-301 ladder for one company and record the answer.

    ``backup=True`` adds the last-resort sources of
    :mod:`dreamjob.pipeline.contact_backup` - the stored postings and the ATS
    board - but only after the ordinary ladder has produced nothing usable
    (FR-303).  A company the ladder can already answer costs no backup fetch.
    """
    outcome = CompanyOutcome(
        company_id=company.get("company_id") or "",
        company_name=company.get("company_name") or "",
        vacancy_count=int(company.get("vacancy_count") or 0),
    )

    # 1 - already reachable?  Then this company costs nothing at all (FR-341).
    known = await asyncio.to_thread(
        contacts_repo.usable_contacts_for_company, outcome.company_id
    )
    known = [row for row in known if (row.get("email") or "").strip()]
    if known:
        best = max(known, key=lambda r: float(r.get("confidence") or 0))
        outcome.status = "reachable"
        outcome.reused = True
        outcome.contact_id = best["id"]
        outcome.email = best.get("email")
        outcome.method = best.get("email_source_method") or "knowledge_base"
        outcome.validation = best.get("email_validation") or validation.UNKNOWN
        outcome.is_generic = bool(best.get("is_generic_mailbox"))
        outcome.domain = patterns.domain_of(best.get("email"))
        # No ``domain_source``: this pass resolved no domain, it reused a
        # contact.  Saying "company" here would credit the derivation gate with
        # work it did not do, and the coverage figures read that column.
        outcome.reason = "already in the knowledge base"
        await asyncio.to_thread(repo.record_resolution, outcome.company_id, _resolution(outcome))
        return outcome

    hints = await asyncio.to_thread(repo.vacancy_hints, outcome.company_id)
    # Rung 2 reads the employer's *own* stated channel, and the six newest
    # postings are not where it necessarily is: a company with twenty-seven
    # vacancies that wrote "stuur je cv naar ..." in the tenth of them was
    # invisible to this ladder.  Asking the database for the postings that
    # contain an "@" at all costs one indexed scan and no network (FR-303).
    with_address = await asyncio.to_thread(
        repo.vacancies_carrying_an_address, outcome.company_id
    )
    seen_hints = {hint.get("id") for hint in hints}
    hints += [hint for hint in with_address if hint.get("id") not in seen_hints]
    profile = await asyncio.to_thread(repo.company_people, outcome.company_id)
    company = {**company, **{k: v for k, v in profile.items() if k in ("key_people", "structure")}}
    if profile.get("careers_url") and not company.get("careers_url"):
        company["careers_url"] = profile["careers_url"]

    published = addresses_in_vacancies(hints)
    domain, source, note = await resolve_domain(
        company, hints, published, egress=egress, derive=derive_domains
    )
    outcome.domain = domain or None
    outcome.domain_source = source or None
    outcome.notes.append(note)

    if domain and source == SOURCE_DERIVED:
        await asyncio.to_thread(repo.set_company_domain, outcome.company_id, domain)

    diagnostics: dict[str, Any] = {}
    candidates = await gather_candidates(
        company, domain, published, egress=egress,
        crawl_site=crawl_site and bool(domain), allow_generic=allow_generic and bool(domain),
        diagnostics=diagnostics,
    )
    chosen = (
        await asyncio.to_thread(first_usable, candidates, allow_smtp=allow_smtp)
        if candidates
        else None
    )
    backup_note = ""
    if chosen is None and backup:
        # The ladder produced nothing usable.  Only now do the backup sources
        # run, so a company the ordinary path can answer costs no fetch at all
        # (FR-303).  The recovered domain's crawl and generics already travel
        # inside the findings, so they are not fetched a second time.
        findings, backup_domain, backup_note = await _walk_backup(
            company, egress=egress, crawl_site=crawl_site
        )
        if findings:
            candidates.extend(backup_candidates(findings))
        if backup_domain and not outcome.domain:
            outcome.domain = backup_domain
            outcome.domain_source = SOURCE_BACKUP
        if backup_note:
            outcome.notes.append(backup_note)
        if candidates:
            chosen = await asyncio.to_thread(first_usable, candidates, allow_smtp=allow_smtp)

    if chosen is None:
        if candidates:
            blocked = sum(1 for c in candidates if c.blocked)
            outcome.reason = (
                f"{len(candidates)} candidate address(es) were found and none survived FR-304"
                + (f"; {blocked} carry an objection (NFR-302)" if blocked else "")
            )
        elif diagnostics.get("challenge_pages"):
            outcome.reason = (
                f"{note or 'the domain resolved'}; the site answered with a bot-protection "
                "challenge (F5/Cloudflare/Incapsula), so no page could be read for an address"
            )
        else:
            outcome.reason = note or "no address could be found for this company"
        if backup_note:
            # ``unreachable_reasons`` must say the backup stage was tried; a
            # bucket that only repeated "no address" would hide the attempt.
            outcome.reason = f"backup attempted: {backup_note}; {outcome.reason}"
        await asyncio.to_thread(repo.record_resolution, outcome.company_id, _resolution(outcome))
        return outcome

    candidate, verdict = chosen
    scope = discovery.storage_scope("http", campaign_id)
    contact_id, _created = await asyncio.to_thread(
        contacts_repo.upsert_contact, candidate.as_contact_row(outcome.company_id, scope)
    )
    # Never point one company's resolution at another company's contact row.
    # ``upsert_contact`` is company-scoped, so this should not fire; the check
    # is the backstop that makes the invariant true even if a future caller
    # forgets the scope (live data had 731 resolutions linked to a contact of a
    # different company, the largest being ``jobs@arbeitnow.fr`` under 497).
    stored = await asyncio.to_thread(contacts_repo.get_contact, contact_id)
    if stored is None or str(stored.get("company_id") or "") != str(outcome.company_id):
        outcome.status = "unreachable"
        outcome.email = None
        outcome.contact_id = None
        outcome.reason = (
            "the best address already belongs to a contact recorded for another "
            "company, so this company is not marked reachable through it"
        )
        await asyncio.to_thread(repo.record_resolution, outcome.company_id, _resolution(outcome))
        return outcome
    candidate.contact_id = contact_id

    outcome.status = "reachable"
    outcome.contact_id = contact_id
    outcome.email = candidate.email
    outcome.method = candidate.email_source_method
    outcome.validation = verdict.result
    outcome.is_generic = candidate.is_generic_mailbox
    outcome.reason = candidate.rationale
    await asyncio.to_thread(repo.record_resolution, outcome.company_id, _resolution(outcome))
    return outcome


def _resolution(outcome: CompanyOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status,
        "contact_id": outcome.contact_id,
        "email": outcome.email,
        "domain": outcome.domain,
        "domain_source": outcome.domain_source,
        "method": outcome.method,
        "validation": outcome.validation,
        "is_generic": outcome.is_generic,
        "reason": outcome.reason[:400],
        "vacancy_count": outcome.vacancy_count,
    }


async def resolve_company_by_id(
    company_id: str,
    *,
    campaign_id: str | None = None,
    allow_smtp: bool = False,
    crawl_site: bool = True,
    derive_domains: bool = True,
    allow_generic: bool = True,
    egress: EgressClient | None = None,
    backup: bool = False,
) -> CompanyOutcome:
    """Walk the FR-301 ladder for one company named by id (FR-301, FR-303).

    The batch pass (:func:`ensure_apply_contacts`) has its own work list and its
    own politeness budget; this is the same ladder asked about one named
    company, which is what the Contacts screen needs when somebody picks a
    company and presses "Find contacts".  The company row is read here rather
    than accepted from the caller, so no caller can hand the ladder a company
    name and domain that do not belong together.

    Raises :class:`LookupError` when the company does not exist, which the
    router turns into a 404.
    """
    company = await asyncio.to_thread(repo.company_for_resolution, company_id)
    if company is None:
        raise LookupError(f"No company {company_id}")

    async def walk(client: EgressClient) -> CompanyOutcome:
        return await resolve_company(
            company,
            egress=client,
            campaign_id=campaign_id,
            allow_smtp=allow_smtp,
            crawl_site=crawl_site,
            derive_domains=derive_domains,
            allow_generic=allow_generic,
            backup=backup,
        )

    if egress is None:
        async with EgressClient(store_raw=False) as client:
            return await walk(client)
    return await walk(egress)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


@dataclass
class ApplyContactsReport:
    """What one pass achieved, in the terms the brief asks for."""

    job_seeker_id: str
    requested: int
    #: ``shortlist`` counts vacancies; ``all`` counts companies.  Recorded so a
    #: checkpoint decoded long after the run says which target it was given.
    scope: str = "shortlist"
    #: Whether this pass ignored :data:`apply.ALL_COMPANIES_FRESHNESS_DAYS`.
    #: Recorded for the same reason ``scope`` is: a report read long after the
    #: run must say which pool the sweep was given, not just what it found.
    retry_recent: bool = False
    already_covered: int = 0
    shortfall: int = 0
    companies_visited: int = 0
    companies_reachable: int = 0
    companies_unreachable: int = 0
    companies_reused: int = 0
    vacancies_covered: int = 0
    by_method: Counter[str] = field(default_factory=Counter)
    by_validation: Counter[str] = field(default_factory=Counter)
    by_domain_source: Counter[str] = field(default_factory=Counter)
    unreachable_reasons: Counter[str] = field(default_factory=Counter)
    outcomes: list[CompanyOutcome] = field(default_factory=list)
    started_at: str = field(default_factory=utcnow)
    finished_at: str = ""

    def absorb(self, outcome: CompanyOutcome) -> None:
        self.companies_visited += 1
        self.outcomes.append(outcome)
        if outcome.reachable:
            self.companies_reachable += 1
            self.vacancies_covered += outcome.vacancy_count
            method = outcome.method or "unknown"
            if outcome.is_generic and method == patterns.METHOD_PATTERN:
                method = "generic_mailbox"
            # A published backup address keeps its own label - ``ats_board`` or
            # ``stored_document`` - so the report never counts it as a composed
            # conventional mailbox.  A composed backup generic stays
            # ``pattern_inference`` and is bucketed as usual.
            self.by_method[method] += 1
            self.by_validation[outcome.validation or validation.UNKNOWN] += 1
            self.by_domain_source[outcome.domain_source or "none"] += 1
            if outcome.reused:
                self.companies_reused += 1
        else:
            self.companies_unreachable += 1
            self.unreachable_reasons[_reason_bucket(outcome.reason)] += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_seeker_id": self.job_seeker_id,
            "requested": self.requested,
            "scope": self.scope,
            "retry_recent": self.retry_recent,
            "already_covered": self.already_covered,
            "shortfall": self.shortfall,
            "companies_visited": self.companies_visited,
            "companies_reachable": self.companies_reachable,
            "companies_unreachable": self.companies_unreachable,
            "companies_reused": self.companies_reused,
            "vacancies_covered": self.vacancies_covered,
            "by_method": dict(self.by_method),
            "by_validation": dict(self.by_validation),
            "by_domain_source": dict(self.by_domain_source),
            "unreachable_reasons": dict(self.unreachable_reasons.most_common(10)),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "corpus": repo.vacancies_with_contact(),
            # The corpus figure the brief asks to be told honestly: every
            # vacancy that has somebody to write to, split by *what kind* of
            # address it is (FR-303) and by the FR-304 verdict on it, so a
            # conventional careers mailbox is never counted as if somebody had
            # published it.
            "coverage": repo.coverage_by_method(),
        }

    def progress(self) -> dict[str, Any]:
        """The counters a progress tick needs, without the corpus queries.

        :meth:`as_dict` is the finished report and reads the repository twice
        for figures that only make sense once the pass is over.  A tick fires
        after every company, so it carries the cheap counters and nothing else.
        """
        return {
            "scope": self.scope,
            "requested": self.requested,
            "companies_visited": self.companies_visited,
            "companies_reachable": self.companies_reachable,
            "companies_unreachable": self.companies_unreachable,
            "companies_reused": self.companies_reused,
            "vacancies_covered": self.vacancies_covered,
            "by_method": dict(self.by_method),
            "by_validation": dict(self.by_validation),
            "unreachable_reasons": dict(self.unreachable_reasons.most_common(5)),
        }


def _reason_bucket(reason: str) -> str:
    """Group the reasons, so the report says what happened rather than listing it.

    Every bucket has to be matched before the fall-through: an unbucketed
    reason names the company it came from, and a hundred of those is a list,
    not a report.
    """
    text = (reason or "").lower()
    if "timed out" in text:
        return "timed out"
    if "no word a domain" in text:
        return "company name carries no identifying word"
    if "too short to spell" in text:
        return "company name is one short word"
    if "none confirmed" in text or "no candidate domain" in text or (
        "confirmed nothing" in text
    ):
        return "no domain could be confirmed"
    if "fr-304" in text:
        return "addresses found but none survived validation"
    if "resolution failed" in text:
        return "resolution failed"
    if not text:
        return "unknown"
    return text[:80]


def _stale_before(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def _notify(cb: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]) -> None:
    """Hand a progress payload to the caller; progress must never fail a pass."""
    if cb is None:
        return
    try:
        cb(payload)
    except Exception:  # noqa: BLE001 - progress must never fail a pass
        log.debug("progress callback failed", exc_info=True)


async def _settle_tasks(tasks: list[asyncio.Task]) -> None:
    """Cancel company tasks and wait a bounded time for them, never for ever.

    The completion path used to be a bare ``gather(..., return_exceptions=True)``
    after cancelling, which waits however long a task takes to unwind.  A task
    that refuses to unwind then pinned the pass - and the job - with it.
    ``asyncio.wait`` returns at the grace deadline whatever the tasks do, so the
    sweep always reaches its terminal tick; stragglers are named in the log and
    left to the loop, which cancels them again at shutdown.
    """
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    done, pending = await asyncio.wait(tasks, timeout=CANCELLATION_GRACE_SECONDS)
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            log.warning("A company task ended with %s after cancellation", exc)
    if pending:
        log.warning(
            "Contacts pass: %d company task(s) did not unwind within %.0fs of "
            "cancellation; settling the job without waiting for them: %s",
            len(pending), CANCELLATION_GRACE_SECONDS,
            [task.get_name() for task in pending],
        )


async def ensure_apply_contacts(
    job_seeker_id: str,
    limit: int = 500,
    *,
    campaign_id: str | None = None,
    scope: str = "shortlist",
    max_companies: int | None = None,
    concurrency: int = 8,
    allow_smtp: bool = False,
    crawl_site: bool = True,
    derive_domains: bool = True,
    allow_generic: bool = True,
    backup: bool = False,
    refresh: bool = False,
    retry_recent: bool = False,
    order: str = "vacancies",
    skip_company_ids: Collection[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> ApplyContactsReport:
    """Give the seeker something to apply to, at one of two scopes (FR-301, FR-303).

    ``scope='shortlist'`` (the default) is the original pass: ``limit`` counts
    *vacancies*, the work list is :func:`apply.companies_needing_contact`, and
    the pass stops as soon as ``limit`` vacancies have somebody to write to.
    Sixty companies can carry five hundred vacancies, so counting companies
    would either stop far too early or crawl hundreds of sites nobody needed.

    ``scope='all'`` widens the pool to every company in the knowledge base,
    including companies with no vacancy and no opportunity: ``limit`` counts
    *companies to visit*, the work list is
    :func:`apply.all_companies_for_contact` - every company that does not yet
    have a usable contact - and the pass visits every selected company without
    stopping early on vacancy coverage.  A company whose last verdict is
    younger than :data:`apply.ALL_COMPANIES_FRESHNESS_DAYS` is left alone, and
    the queue puts never-attempted companies first and the oldest attempts
    next, so a repeated sweep spends its budget on new companies instead of
    re-walking the ones it just walked.  ``refresh`` ignores that backoff and
    includes companies that already have a contact, so they are re-checked;
    ``retry_recent`` ignores the backoff *without* including covered companies,
    which is the pool a caller wants when every company still lacking a contact
    was attempted within the window - the backoff then hides the whole pool,
    but a refresh would also spend the pass on companies that need nothing.
    ``max_companies`` caps the work list either way.

    ``order`` is ``vacancies`` by default because ``limit`` is: a target
    counted in vacancies is filled fastest by walking the companies that carry
    the most of them, and one site crawl that covers twenty-seven vacancies is
    twenty-seven times the coverage of one that covers a single posting for the
    same politeness budget (FR-305, CR-402).  ``recency`` is still there for a
    caller that wants the freshest corpus rather than the largest.

    In the ``shortlist`` scope, ``limit`` is a *target for the corpus*, not a
    quota for this call: the vacancies already covered are subtracted first, so
    calling this twice does not do the work twice, and calling it when the
    corpus is already there fetches nothing at all.  ``already_covered`` and
    ``shortfall`` on the report say which of the two happened; in the ``all``
    scope ``requested`` is the number of companies selected instead.

    Companies are resolved concurrently; the shared :class:`EgressClient` is
    what keeps that polite, since its per-domain rate limit and robots.txt
    cache are per client, not per task (FR-182, FR-305).  Every company is
    a different domain, so the concurrency costs no single site anything.

    ``skip_company_ids`` is the resume seam (NFR-401).  A worker that survives
    a restart keeps the companies it already visited in its checkpoint and
    passes them here, so the sweep neither re-walks nor re-counts them.  The
    work list is asked for ``ceiling + len(skip_company_ids)`` rows and the
    skipped companies are dropped afterwards, which keeps the selected set the
    same size it would have been without the skips rather than shrinking it.
    When every selected company is skipped the pass emits its terminal
    ``done`` tick and returns an empty report rather than failing.
    """
    sweep_all = scope == "all"
    skip = {str(company_id) for company_id in (skip_company_ids or ()) if company_id}
    # The corpus figures below used to be read before any progress was emitted,
    # and the corpus queries could take minutes; the screen sat blank for the
    # whole of it.  Say what is happening first, with no total yet.
    _notify(
        on_progress,
        {"phase": "preparing", "done": 0, "total": None, "report": {}},
    )
    covered_now = await asyncio.to_thread(repo.vacancies_with_contact)
    report = ApplyContactsReport(
        job_seeker_id=job_seeker_id,
        requested=limit,
        scope=scope,
        retry_recent=retry_recent,
        already_covered=int(covered_now["with_contact"]),
    )

    if sweep_all:
        # ``limit`` counts companies here, so there is no corpus shortfall to
        # reach and no early return: the sweep visits every company the work
        # list selects, whatever the vacancy coverage turns out to be.
        ceiling = limit if max_companies is None else min(limit, max_companies)
        work = await asyncio.to_thread(
            repo.all_companies_for_contact,
            ceiling + len(skip),
            job_seeker_id=job_seeker_id,
            include_covered=refresh,
            ignore_backoff=retry_recent,
            order=order,
        )
        if skip:
            work = [
                row for row in work
                if str(row.get("company_id") or "") not in skip
            ][:ceiling]
        # ``requested`` reports how many companies the sweep set out to visit,
        # which is what a caller comparing it to ``companies_visited`` wants.
        report.requested = len(work)
        report.shortfall = 0
    else:
        report.shortfall = max(0, limit - report.already_covered)
        if report.shortfall == 0:
            log.info(
                "Apply contacts: %d vacancies already have a contact; nothing to do",
                report.already_covered,
            )
            report.finished_at = utcnow()
            _notify(
                on_progress,
                {"phase": "done", "done": 1, "total": 1, "report": report.progress()},
            )
            return report
        # Two companies looked at per vacancy still needed: about half of them
        # turn out to be unreachable, and the reachable ones carry more than one
        # vacancy each, so this is a generous ceiling rather than a tight
        # estimate.
        ceiling = max_companies if max_companies is not None else max(50, 2 * report.shortfall)
        work = await asyncio.to_thread(
            repo.companies_needing_contact,
            ceiling + len(skip),
            job_seeker_id=job_seeker_id,
            include_resolved=refresh,
            resolved_before=_stale_before(RESOLUTION_MAX_AGE_DAYS) if refresh else None,
            order=order,
        )
        if skip:
            work = [
                row for row in work
                if str(row.get("company_id") or "") not in skip
            ][:ceiling]
    total = len(work)
    if not work:
        report.finished_at = utcnow()
        _notify(
            on_progress,
            {"phase": "done", "done": 1, "total": 1, "report": report.progress()},
        )
        return report

    _notify(
        on_progress,
        {"phase": "start", "done": 0, "total": total, "report": report.progress()},
    )

    gate = asyncio.Semaphore(max(1, concurrency))
    done = asyncio.Event()

    async with EgressClient(store_raw=False) as client:

        async def one(company: dict[str, Any]) -> CompanyOutcome | None:
            if done.is_set():
                return None
            async with gate:
                if done.is_set():
                    return None
                try:
                    return await asyncio.wait_for(
                        resolve_company(
                            company,
                            egress=client,
                            campaign_id=campaign_id,
                            allow_smtp=allow_smtp,
                            crawl_site=crawl_site,
                            derive_domains=derive_domains,
                            allow_generic=allow_generic,
                            backup=backup,
                        ),
                        timeout=COMPANY_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    # A company that never answers is an outcome, not a stopped
                    # pass: record it unreachable and let the sweep finish.
                    log.warning(
                        "Contact resolution timed out after %.0fs for company %s",
                        COMPANY_TIMEOUT_SECONDS, company.get("company_id"),
                    )
                    outcome = CompanyOutcome(
                        company_id=company.get("company_id") or "",
                        company_name=company.get("company_name") or "",
                        vacancy_count=int(company.get("vacancy_count") or 0),
                        reason=f"timed out after {COMPANY_TIMEOUT_SECONDS:.0f}s",
                    )
                    try:
                        await asyncio.to_thread(
                            repo.record_resolution, outcome.company_id, _resolution(outcome)
                        )
                    except Exception:  # noqa: BLE001 - recording must not stop the pass
                        log.warning(
                            "Could not record the timed-out resolution for company %s",
                            outcome.company_id, exc_info=True,
                        )
                    return outcome
                except Exception as exc:  # noqa: BLE001 - one company never stops a pass
                    log.exception(
                        "Contact resolution failed for company %s", company.get("company_id")
                    )
                    return CompanyOutcome(
                        company_id=company.get("company_id") or "",
                        company_name=company.get("company_name") or "",
                        vacancy_count=int(company.get("vacancy_count") or 0),
                        reason=f"resolution failed: {type(exc).__name__}: {exc}"[:200],
                    )

        tasks = [
            asyncio.create_task(one(company), name=f"contacts:{company.get('company_id')}")
            for company in work
        ]
        try:
            for task in asyncio.as_completed(tasks):
                outcome = await task
                if outcome is None:
                    continue
                report.absorb(outcome)
                _notify(
                    on_progress,
                    {
                        "phase": "company",
                        "done": report.companies_visited,
                        "total": total,
                        "company_id": outcome.company_id,
                        "report": report.progress(),
                    },
                )
                if not sweep_all and report.vacancies_covered >= report.shortfall:
                    done.set()
        finally:
            await _settle_tasks(tasks)

    report.finished_at = utcnow()
    # The terminal tick.  Every early return above emits one; the normal path
    # must too, otherwise a UI that keys completion off ``phase`` sits on the
    # last company event for ever - which is exactly how the live sweep read as
    # stuck even after the work had stopped.  Same shape as the early returns:
    # a terminal marker, with the counters already carried by the last company
    # event.
    _notify(
        on_progress,
        {"phase": "done", "done": 1, "total": 1, "report": report.progress()},
    )
    log.info(
        "Apply contacts (%s): %d/%d vacancies newly covered across %d companies "
        "(%d reachable, %d unreachable, %d reused); %d were covered before the pass",
        scope, report.vacancies_covered, report.shortfall, report.companies_visited,
        report.companies_reachable, report.companies_unreachable, report.companies_reused,
        report.already_covered,
    )
    return report


def run_ensure_apply_contacts(job_seeker_id: str, limit: int = 500, **kwargs: Any) -> dict[str, Any]:
    """Synchronous entry point, for scripts and the job runner."""
    return asyncio.run(ensure_apply_contacts(job_seeker_id, limit, **kwargs)).as_dict()


# ---------------------------------------------------------------------------
# The job the Contacts screen starts (FR-185, NFR-401, NFR-502)
# ---------------------------------------------------------------------------

#: Job kind for a contacts pass started from the Contacts screen.  Registered
#: at import time to match every other job kind, so a run interrupted by a
#: restart is picked up again rather than abandoned.  The pass is idempotent -
#: companies already covered cost nothing and resolved companies are skipped -
#: so a resumed run continues rather than duplicating work.
DISCOVERY_JOB_KIND = "contacts_discovery"

#: The visited-company checkpoint is the resume list for a whole sweep and can
#: reach ~100 KB.  Writing it on every company made every progress tick a
#: hundred-kilobyte transaction; it is now written every this many companies or
#: :data:`VISITED_CHECKPOINT_SECONDS`, whichever comes first, plus once when the
#: pass ends (or is cancelled) so a restart never loses more than a batch.
VISITED_CHECKPOINT_COMPANIES = 10
VISITED_CHECKPOINT_SECONDS = 30.0


async def contacts_discovery_worker(ctx: JobContext):
    """Run the FR-301 pass for a seeker, reporting the coverage it reached.

    The caller stores the pass options in the job checkpoint (``{"options":
    {...}}``), which is what makes the worker resumable without a closure.
    ``visited_company_ids`` in the same checkpoint is the other half of that:
    every company the callback reports is appended and persisted in batches
    (every :data:`VISITED_CHECKPOINT_COMPANIES` companies or
    :data:`VISITED_CHECKPOINT_SECONDS` seconds, and once more when the pass
    ends), and a restarted worker passes the set back to
    :func:`ensure_apply_contacts` so the sweep continues instead of starting
    over (NFR-401).  The UI counter is therefore reported as ``base + done``,
    where ``base`` is how many companies earlier runs already visited, so a
    resumed bar keeps moving forward rather than jumping back to zero.
    """
    options = dict((ctx.checkpoint or {}).get("options") or {})
    limit = int(options.pop("limit", 500) or 500)
    campaign_id = options.pop("campaign_id", None) or ctx.campaign_id
    # The API stores the flag under its request name; the pass calls it
    # ``backup``.  Translating here keeps the checkpoint faithful to the
    # request that created it and the call signature faithful to FR-303.
    if "backup_methods" in options:
        options["backup"] = bool(options.pop("backup_methods"))

    visited = [str(cid) for cid in (ctx.checkpoint or {}).get("visited_company_ids") or []]
    visited_set = set(visited)
    base = len(visited)
    unflushed = 0
    last_write = time.monotonic()

    def flush_visited(*, force: bool = False) -> None:
        """Persist the visited list, at most once per batch or window."""
        nonlocal unflushed, last_write
        if not visited:
            return
        now = time.monotonic()
        if not force and unflushed < VISITED_CHECKPOINT_COMPANIES and (
            now - last_write < VISITED_CHECKPOINT_SECONDS
        ):
            return
        ctx.save_checkpoint(visited_company_ids=visited[-30000:])
        unflushed = 0
        last_write = now

    def on_progress(payload: dict[str, Any]) -> None:
        nonlocal unflushed, last_write
        company_id = payload.get("company_id")
        if company_id:
            company_id = str(company_id)
            if company_id not in visited_set:
                visited.append(company_id)
                visited_set.add(company_id)
                unflushed += 1
        total = payload.get("total")
        if total:
            done = int(payload.get("done") or 0)
            if payload.get("phase") in ("start", "company"):
                ctx.progress(base + done, base + int(total))
            else:
                ctx.progress(done, int(total))
        extra: dict[str, Any] = {}
        if payload.get("report"):
            extra["report"] = payload["report"]
        now = time.monotonic()
        if company_id and (
            unflushed >= VISITED_CHECKPOINT_COMPANIES
            or now - last_write >= VISITED_CHECKPOINT_SECONDS
        ):
            extra["visited_company_ids"] = visited[-30000:]
            unflushed = 0
            last_write = now
        if extra:
            ctx.save_checkpoint(**extra)

    try:
        report = await ensure_apply_contacts(
            ctx.job_seeker_id or "", limit, campaign_id=campaign_id,
            skip_company_ids=visited_set, on_progress=on_progress, **options,
        )
    finally:
        # A cancelled or failed pass must not lose the companies it did visit;
        # the final flush is also what makes a crash resume from the last batch
        # rather than from the start.
        flush_visited(force=True)
    payload = report.as_dict()
    ctx.save_checkpoint(report=payload)
    ctx.progress(1, 1)
    yield payload


runner.register_worker(DISCOVERY_JOB_KIND, contacts_discovery_worker)
