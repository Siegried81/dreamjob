"""Find the website behind a company we already know something about.

The company-enrichment ladder needs one thing before it can do anything: a URL
to read.  Without it the website rung cannot classify the employer, the
crawler has no page to fetch, and a company stays a name - so scoring, which
reads the summary, sector, size, stage and values the crawl produces, scores
it against nothing.  Measured on this installation: 57 of 66 employers behind
one seeker's opportunities had no ``domain``, and the enrichment reported
"profiled: 64" while storing almost nothing, because there was nothing to read.

Most of those companies are not actually unknown.  We hold a vacancy whose
``source_url`` is on the employer's own site, or an ATS board slug whose tenant
name is the employer's name.  This module turns that into a domain:

* **A vacancy URL on a real host** is the company's site already
  (``jobs.monizze.be`` -> ``monizze.be``).  No request needed.
* **An ATS board URL** names its tenant (``aikidosecurity.recruitee.com`` ->
  ``aikidosecurity``), and the tenant name is usually the company name, so the
  plausible top-level domains are probed and the first live one wins.
* **A stored ``source`` that is a URL** is used as-is.

Nothing is guessed into the knowledge base: a candidate is probed, and only a
host that answers is written.  A wrong domain is worse than a missing one - it
would profile somebody else's company under this one's name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.website.crawler import registrable_domain
from dreamjob.db.connection import query_all, update_row, utcnow

log = logging.getLogger(__name__)

#: Multi-tenant hosts.  The registrable domain of a URL on one of these is the
#: *platform's*, not the employer's, so it must never be stored as the company's
#: website - doing that would profile Recruitee for every one of its tenants.
ATS_HOSTS: dict[str, str] = {
    "recruitee.com": "{slug}.recruitee.com",
    "personio.de": "{slug}.jobs.personio.de",
    "personio.com": "{slug}.jobs.personio.com",
    "greenhouse.io": "boards.greenhouse.io/{slug}",
    "lever.co": "jobs.lever.co/{slug}",
    "smartrecruiters.com": "{slug}.smartrecruiters.com",
    "ashbyhq.com": "jobs.ashbyhq.com/{slug}",
    "workable.com": "apply.workable.com/{slug}",
    "teamtailor.com": "{slug}.teamtailor.com",
    "jobtoolz.com": "{slug}.jobtoolz.com",
    "bamboohr.com": "{slug}.bamboohr.com",
    "myworkdayjobs.com": "{slug}.myworkdayjobs.com",
    "icims.com": "{slug}.icims.com",
    "taleo.net": "{slug}.taleo.net",
    "successfactors.eu": "{slug}.successfactors.eu",
    "zoho.com": "{slug}.zohorecruit.com",
    "hrflow.ai": "{slug}.hrflow.ai",
}

#: Tried in order when the tenant name is used as a domain label.  ``.be``
#: first because the campaigns here are Belgian; the rest are common enough
#: that a technology company is usually one of them.
TLD_CANDIDATES = (".be", ".com", ".io", ".dev", ".eu", ".net", ".ai", ".co")

#: Aggregators and public bodies.  A vacancy URL on one of these says where the
#: advertisement was syndicated, not who the employer is - and accepting it
#: wrote ``fgov.be`` as the website of three different Belgian companies and
#: ``europa.eu`` as the website of two more before this list existed.  A wrong
#: domain is worse than none: it profiles a stranger under this name.
JOB_BOARD_HOSTS: tuple[str, ...] = (
    "europa.eu", "fgov.be", "ictjob.be", "indeed.com", "stepstone.be",
    "stepstone.com", "jobat.be", "vdab.be", "actiris.be", "leforem.be",
    "linkedin.com", "glassdoor.com", "glassdoor.be", "monster.com",
    "arbeitnow.com", "welcometothejungle.com", "jobs.belgie.be",
    "interimjobs.be", "randstad.be", "adecco.be", "manpower.be",
    "jobsinbrussels.be", "eurojobs.com", "eures.europa.eu", "talent.com",
    "jobrapido.com", "careerjet.be", "jooble.org", "xing.com", "yourtalent.be",
    "remotive.com", "remoteok.com", "jobicy.com", "himalayas.app", "adzuna.com",
    "adzuna.be", "adzuna.nl", "adzuna.fr", "adzuna.de", "adzuna.co.uk", "ycombinator.com",
)


def is_aggregator(host: str) -> bool:
    host = (host or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in JOB_BOARD_HOSTS)


def _name_matches_host(company: dict, host: str) -> bool:
    """Does this host plausibly belong to this company?

    The guard that keeps a syndicated advert from being mistaken for a website:
    the host's own label has to share something with the company's name.
    ``DIGNIFY`` and ``fgov.be`` share nothing; ``Relu`` and ``relu.be`` do.
    Short names (three letters or fewer) are exempt, because a token test on
    them is noise.
    """
    label = registrable_domain(host).split(".")[0]
    if len(label) <= 3:
        return True
    name_tokens = _tokens(str(company.get("name") or ""))
    if not name_tokens:
        return True
    return any(token.startswith(label) or label.startswith(token) for token in name_tokens)


def _tokens(text: str) -> list[str]:
    words = "".join(ch if ch.isalnum() else " " for ch in text.lower()).split()
    return [w for w in words if len(w) >= 4]

#: A board slug like "acme-jobs" names the employer "acme".
_SLUG_NOISE = ("-jobs", "-careers", "careers", "jobs", "-", "_")


def ats_tenant(host: str) -> str | None:
    """The employer's name as its ATS board spells it, or ``None``."""
    host = (host or "").lower()
    for platform in ATS_HOSTS:
        if host == platform or host.endswith("." + platform):
            label = host[: -(len(platform) + 1)] if host != platform else ""
            # ``technopolis-group.jobs.personio.de`` -> the tenant is the FIRST
            # segment; taking the last one yields "jobs", the platform's own
            # subdomain, which is not a company name.
            label = label.split(".")[0] if "." in label else label
            return _clean_label(label) or None
    return None


def is_ats_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == p or host.endswith("." + p) for p in ATS_HOSTS)


def _clean_label(label: str) -> str:
    label = (label or "").strip().lower()
    for noise in _SLUG_NOISE:
        label = label.replace(noise, "") if noise.startswith("-") else label
    if label.endswith("jobs") or label.endswith("careers"):
        label = label[: -len("jobs")] if label.endswith("jobs") else label[: -len("careers")]
    return "".join(ch for ch in label if ch.isalnum())


def candidates(company: dict, vacancy_urls: list[str]) -> list[str]:
    """Hosts worth trying for this company, best first.  No requests made."""
    out: list[str] = []

    def add(host: str) -> None:
        host = (host or "").strip().lower().removeprefix("www.")
        if not host or host in out or is_ats_host(host) or is_aggregator(host):
            return
        out.append(host)

    # 1. A URL we already hold that is on the company's own site.  Aggregators
    #    are excluded and the host must relate to the company's name, because a
    #    syndicated advert's host is the board's, not the employer's.
    for url in vacancy_urls:
        parsed = urlparse(url if "//" in url else f"https://{url}")
        host = registrable_domain(parsed.netloc) if parsed.netloc else ""
        if not host or is_ats_host(host) or is_aggregator(host):
            continue
        if not _name_matches_host(company, host):
            continue
        add(host)

    # 2. The ATS tenant name, as plausible domains.
    tenants = [t for t in (ats_tenant(urlparse(u if "//" in u else f"https://{u}").netloc)
                           for u in vacancy_urls) if t]
    if company.get("ats_slug"):
        tenants.insert(0, _clean_label(str(company["ats_slug"])))
    for tenant in tenants[:1]:
        if tenant and len(tenant) >= 3:
            for tld in TLD_CANDIDATES:
                add(tenant + tld)

    return out


def board_url(company: dict) -> str | None:
    """The employer's ATS board, for the crawl to start from (FR-221).

    A board is a real page about the employer: it carries the name, often a
    logo and a link out, and its job descriptions name the products and the
    stack.  It is a poorer source than the company's own site, so it is used
    only when there is no site to read - never instead of one.
    """
    vendor = (company.get("ats_vendor") or "").strip().lower()
    slug = (company.get("ats_slug") or "").strip()
    if not vendor or not slug:
        return None
    # ``ats_vendor`` is stored as the bare platform name ("personio",
    # "recruitee") while the host table is keyed by its domain ("personio.de"),
    # so match on the label rather than demanding the two be spelled the same.
    pattern = None
    for platform, template in ATS_HOSTS.items():
        label = platform.split(".")[0]
        if vendor in (platform, label) or label.startswith(vendor):
            pattern = template
            break
    if not pattern:
        return None
    path = pattern.format(slug=slug)
    return path if path.startswith("http") else f"https://{path}"


async def resolve_company(
    company: dict, vacancy_urls: list[str], egress: Any
) -> str | None:
    """The company's own host, verified to answer, or ``None``."""
    for host in candidates(company, vacancy_urls):
        url = f"https://{host}"
        try:
            # A probe is not a crawl: it asks whether the host is there, reads
            # nothing of the site, and must not be refused because a robots file
            # was momentarily unreachable - which is what lost real domains.
            result = await egress.fetch(
                url,
                use_cache=False,
                access_method="domain_probe",
                respect_robots=False,
                max_retries=1,
            )
        except Exception:  # noqa: BLE001 - an unreachable candidate is just wrong
            continue
        if result.ok:
            return host
    return None


async def backfill(
    limit: int = 200, *, egress: Any = None, company_ids: list[str] | None = None
) -> dict[str, Any]:
    """Fill in the domain of companies that have vacancies but no site.

    Bounded and resumable by construction: it reads the companies still missing
    a domain, so a second run continues where the first stopped rather than
    repeating work.  ``company_ids`` narrows it to a known set, which is how the
    enrichment pass resolves the companies it is about to profile.
    """
    from dreamjob.egress.client import EgressClient  # noqa: PLC0415

    where = "(c.domain IS NULL OR c.domain = '')"
    params: list[Any] = []
    if company_ids:
        marks = ",".join("?" for _ in company_ids)
        where += f" AND c.id IN ({marks})"
        params.extend(company_ids)
    params.append(int(limit))

    rows = await asyncio.to_thread(
        query_all,
        f"""
        SELECT c.id, c.name, c.domain, c.ats_vendor, c.ats_slug, c.source,
               (SELECT v.source_url FROM vacancy v
                 WHERE v.company_id = c.id AND v.source_url IS NOT NULL LIMIT 1) AS url1,
               (SELECT v.application_target FROM vacancy v
                 WHERE v.company_id = c.id AND v.application_target IS NOT NULL LIMIT 1) AS url2
        FROM company c
        WHERE {where}
          AND EXISTS (SELECT 1 FROM vacancy v WHERE v.company_id = c.id)
        LIMIT ?
        """,
        tuple(params),
    )

    resolved = 0
    boards = 0
    async with EgressClient() as client:
        for row in rows:
            urls = [u for u in (row.get("url1"), row.get("url2"), row.get("source")) if u]
            host = await resolve_company(row, urls, egress or client)
            if host:
                await asyncio.to_thread(
                    update_row, "company", row["id"], {"domain": host, "refreshed_at": utcnow()}
                )
                resolved += 1
                continue
            # No site: record the board so the crawl has somewhere to start.
            board = board_url(row)
            if board:
                await asyncio.to_thread(update_row, "company", row["id"], {"careers_url": board})
                boards += 1
    log.info("Domain backfill: %d resolved, %d given a board", resolved, boards)
    return {"considered": len(rows), "resolved": resolved, "board_only": boards}
