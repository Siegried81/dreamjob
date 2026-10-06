"""Job-alert e-mails from the seeker's own mailbox, turned into vacancies (FR-261, FR-401).

LinkedIn, ictjob.be, Glassdoor and Jobat all forbid or block automated
collection of their sites, and all of them offer what the seeker actually needs
instead: job-alert e-mails.  Reading those e-mails in the seeker's own inbox is
ordinary use of a service the seeker subscribed to, so this module is the lawful
route to those sources:

*It never visits linkedin.com, ictjob.be, glassdoor.com or any jobat.be host.*
It reads messages already delivered to a mailbox the seeker connected - Gmail
with ``gmail.readonly``, the scope the mail slice already requests, or another
provider such as Yahoo over read-only IMAP with an app password
(:class:`ImapAlertMailbox`) - and extracts each posting's title, employer,
location and link from the alert's own HTML, and stores the link as
``source_url`` for the seeker to open by hand.

*One source is a lead detector, not a link harvester.*  Jobat's alert names the
postings but routes every link through one opaque redirector that holds neither
a URL nor a posting id, so the only way to the offer's address would be to
follow that redirect - a request to a Jobat host, which
``docs/Data_Gathering_Plan.md`` section 6.1 rules out.  Its rows therefore carry
title, employer and place with no ``source_url`` at all, and say so in
``application_channel`` (:data:`LEAD_CHANNEL`): the opening is real, the offer
is for the seeker to look up.  Nothing in this module ever requests a Jobat URL.

*Only alert senders are read.*  The Gmail search names the alert senders, and
every fetched message is checked again against :data:`ALERT_SOURCES` before it
is parsed, so a personal e-mail is never turned into a vacancy.

*Deterministic, no model.*  An alert is a short list of links with a title and
an "Employer · Location" line under each; a link pattern per source picks the
postings out, and for the one source whose links say nothing the card's own
shape does.  Nothing from the mailbox is sent to an LLM.

*Same path as every other new posting.*  Records go through the
knowledge-base writer, only rows it *created* count as new (a re-sent alert is
a merge), and new rows reach each live campaign through
:func:`watchlist.add_to_ranked_list` - directives filter them, scoring ranks
them - with the ``vacancy:<id>`` notification the refresh pass uses.

The link patterns follow the alert layouts as published; the ictjob.be one in
particular should be confirmed against a real alert, and a layout change shows
up as ``alerts_without_postings`` in the pass report rather than as silence.
The Jobat card rules were read off delivered alerts (French edition).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.base import NormalisedRecord
from dreamjob.adapters.vacancy_source import (
    application_route,
    country_from_location,
    normalise_work_arrangement,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import dispatch as dispatch_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.db.repositories import seekers as seekers_repo
from dreamjob.mail.inbox import _first_address, _received_at
from dreamjob.monitoring import watchlist
from dreamjob.pipeline import knowledge_base
from dreamjob.pipeline.vacancy_refresh import notify_added

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertSource:
    """One alert sender: who sends it and how its posting links look."""

    key: str
    label: str
    sender_domain: str
    #: Local parts of the sender address that carry job alerts; empty = any.
    sender_prefixes: tuple[str, ...]
    #: Matches a posting link and captures its id, or ``None`` when the alert
    #: carries no usable posting link at all (see :attr:`is_lead_only`).
    link_re: re.Pattern[str] | None
    #: The canonical posting URL built from ``{id}`` (and the link's ``{host}``),
    #: or ``None`` to keep the matched URL without its tracking query.
    canonical: str | None
    #: ``title_first`` (LinkedIn, ictjob), ``employer_first`` (Glassdoor:
    #: employer, rating, title, place inside the posting link) or
    #: ``lead_cards`` (Jobat: no posting link, cards read by their shape).
    layout: str = "title_first"

    @property
    def is_lead_only(self) -> bool:
        """True when this sender's alert names postings but links to none.

        It is derived from ``link_re`` rather than declared separately so the
        two cannot disagree: no link pattern means no posting URL can be
        produced, and a row from such an alert is a lead the seeker has to
        follow up by hand, not something she can open.
        """
        return self.link_re is None


ALERT_SOURCES: tuple[AlertSource, ...] = (
    AlertSource(
        key="alert.linkedin",
        label="LinkedIn job alert",
        sender_domain="linkedin.com",
        sender_prefixes=("jobalerts", "jobs-noreply", "jobs-listings"),
        link_re=re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.IGNORECASE),
        canonical="https://www.linkedin.com/jobs/view/{id}/",
    ),
    AlertSource(
        key="alert.ictjob",
        label="ictjob.be job alert",
        sender_domain="ictjob.be",
        sender_prefixes=(),
        # ictjob posting URLs end in a numeric id after a slug.
        link_re=re.compile(r"ictjob\.be/[^\s\"'<>?#]*?[-/](\d{5,})(?=[/?#\"'\s]|$)", re.IGNORECASE),
        canonical=None,
    ),
    AlertSource(
        key="alert.glassdoor",
        label="Glassdoor job alert",
        sender_domain="glassdoor.com",
        sender_prefixes=("noreply",),
        # Every posting link carries a stable jobListingId among its tracking
        # parameters; the canonical URL keeps only that.
        link_re=re.compile(
            r"glassdoor\.[a-z.]+/partner/jobListing\.htm\?[^\s\"'<>]*?jobListingId=(\d+)",
            re.IGNORECASE,
        ),
        canonical="https://{host}/partner/jobListing.htm?jobListingId={id}",
        layout="employer_first",
    ),
    AlertSource(
        key="alert.jobat",
        label="Jobat job alert",
        sender_domain="jobat.be",
        # The alert arrives from jobat@jobs.jobat.be, but the prefix is left
        # open on purpose: if Jobat renames the sender, the mails still reach
        # the parser and a layout surprise shows up in
        # ``alerts_without_postings`` instead of as silence.
        sender_prefixes=(),
        # Every link in a Jobat alert - navigation and postings alike - is the
        # same opaque redirector (``interactief.jobat.be/optiext/...?ID=<blob>``)
        # and the blob holds neither a URL nor a posting id.  Resolving it would
        # mean requesting a Jobat host, which Data_Gathering_Plan section 6.1
        # rules out, so this source deliberately has no link pattern and
        # produces rows with no ``source_url``.
        link_re=None,
        canonical=None,
        layout="lead_cards",
    ),
)

#: Gmail search for the alert senders; ``after:`` or ``newer_than:`` is added.
GMAIL_QUERY = (
    "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com "
    "OR jobs-listings@linkedin.com OR ictjob.be OR noreply@glassdoor.com "
    "OR jobat.be)"
)

#: How far back the first pass of a newly connected mailbox looks.
FIRST_PASS_WINDOW = "newer_than:14d"
#: Alerts read per mailbox per pass.
MAX_MESSAGES = 50
#: An alert snippet is a title, an employer and a place - no description.
ALERT_CONFIDENCE = 0.6
#: ``application_channel`` for a posting whose alert carried no link.  The
#: column is how the row says what can be done with it (``email``, ``ats_form``,
#: ``url``, ``speculative``); none of those fits a posting that really is
#: advertised but whose address we do not have, and an empty ``url`` channel
#: would read as a broken link rather than as work left to do.  So the row says
#: so in the column the seeker already reads: the opening is real, the offer has
#: to be looked up on the board by hand.
LEAD_CHANNEL = "manual_search"
SETTING_CURSOR_PREFIX = "job_alerts.cursor."

#: Link texts that are buttons, not posting titles, in the alert languages.
_BUTTON_TEXTS = {
    "view job", "view jobs", "see all jobs", "apply", "easy apply", "apply now",
    "voir l'offre", "voir les offres", "postuler", "candidature simplifiée",
    "bekijk vacature", "bekijk vacatures", "solliciteren", "vacature bekijken",
    "more", "plus", "meer", "candidature facile", "afficher plus", "show more",
    "snel solliciteren", "toon meer",
}


def source_for(from_address: str) -> AlertSource | None:
    """The alert source that sent this address, or ``None`` for anything else."""
    address = (from_address or "").lower()
    local, _, domain = address.partition("@")
    for source in ALERT_SOURCES:
        if domain != source.sender_domain and not domain.endswith("." + source.sender_domain):
            continue
        if source.sender_prefixes and not local.startswith(source.sender_prefixes):
            continue
        return source
    return None


class _LinkText(HTMLParser):
    """Flattens HTML into ``(text, href)`` pieces; ``href`` is the enclosing link."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[tuple[str, str | None]] = []
        self._hrefs: list[str | None] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "head"):
            self._skip += 1
        elif tag == "a":
            self._hrefs.append(dict(attrs).get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._hrefs:
            self._hrefs.pop()

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self._skip:
            self.pieces.append((text, self._hrefs[-1] if self._hrefs else None))


def _html_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("html",))
    return part.get_content() if part is not None else ""


def _source_host(source: AlertSource, href: str) -> str | None:
    """A host inside ``href`` belonging to the source's own domain, if there is one.

    Read out of the URL text rather than from ``urlparse().netloc`` because the
    link patterns deliberately match the source's host *anywhere* in the href:
    alert mail routes its links through a tracking redirector, so the netloc is
    the tracker's and the source's real host sits in a query parameter.
    """
    stem = re.escape(source.sender_domain.split(".")[0])
    match = re.search(
        rf"(?:^|[/@.])((?:[\w-]+\.)*{stem}\.[a-z]{{2,}}(?:\.[a-z]{{2,}})?)(?=[/:?#]|$)",
        href,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _posting_url(source: AlertSource, href: str, posting_id: str) -> str:
    """The URL stored for one posting found in an alert mail.

    A ``canonical`` template needs a host, and it must be one the source
    actually publishes on.  Taking it from the href's netloc synthesised a URL
    on whatever host the mail linked through - for a tracked link, the
    redirector's - producing a dead link that looks canonical.  The host is
    read from the source's own domain inside the href, and falls back to the
    sender domain, which every locale of the site redirects from.
    """
    if source.canonical:
        host = _source_host(source, href) or source.sender_domain
        return source.canonical.format(id=posting_id, host=host)
    return href.split("?", 1)[0].split("#", 1)[0]


def _employer_and_place(texts: list[str]) -> tuple[str | None, str | None]:
    """Read the "Employer · Location" line (or two lines) under a title."""
    parts: list[str] = []
    for text in texts:
        parts.extend(p.strip() for p in re.split(r"\s[·•|]\s|\s-\s", text) if p.strip())
        if len(parts) >= 2:
            break
    employer = parts[0] if parts else None
    place = parts[1] if len(parts) > 1 else None
    return employer, place


def parse_alert(message: EmailMessage, source: AlertSource) -> list[dict[str, Any]]:
    """The postings one alert lists, in the order it lists them.

    Each posting is the first link carrying a posting id; its title is the
    longest non-button text inside links with that id, and its employer and
    place are the plain texts right after that title, up to the next link.
    Reading after the title rather than after the posting's last link matters:
    the last link is usually a "View job" button, and what follows it is the
    next posting.

    A lead-only source (:attr:`AlertSource.is_lead_only`) has no posting id to
    group by and is read by :func:`_lead_cards` instead.
    """
    parser = _LinkText()
    parser.feed(_html_of(message))
    pieces = parser.pieces

    if source.is_lead_only:
        return _lead_cards(pieces)

    order: list[str] = []
    urls: dict[str, str] = {}
    linked: dict[str, list[tuple[str, int]]] = {}
    for index, (text, href) in enumerate(pieces):
        match = source.link_re.search(href or "")
        if not match:
            continue
        posting_id = match.group(1)
        if posting_id not in urls:
            order.append(posting_id)
            urls[posting_id] = _posting_url(source, href or "", posting_id)
        linked.setdefault(posting_id, []).append((text, index))

    postings: list[dict[str, Any]] = []
    for posting_id in order:
        if source.layout == "employer_first":
            posting = _read_employer_first([t for t, _ in linked[posting_id]])
        else:
            posting = _read_posting(linked[posting_id], pieces)
        if posting is None:
            continue
        postings.append({"posting_id": posting_id, "source_url": urls[posting_id], **posting})
    return postings


#: Link texts in an employer-first card that are neither employer, title nor
#: place: a star rating, a salary estimate and its "(employer estimate)" note.
_CARD_NOISE_RE = re.compile(
    r"^(\d(?:[.,]\d)?\s*★|[()]|.*€.*|.*\$.*|estimation .*|.*estimate.*|schatting .*)$",
    re.IGNORECASE,
)


def _read_employer_first(texts: list[str]) -> dict[str, Any] | None:
    """Employer, title and place from a Glassdoor card, in that order.

    The rating, the salary estimate and the "Easy apply" button sit in the same
    link and are dropped; the salary is not kept because Glassdoor's ranges are
    estimates (one alert read "52 k € - 1 M €").
    """
    words = [t for t in texts if t.lower() not in _BUTTON_TEXTS and not _CARD_NOISE_RE.match(t)]
    if len(words) < 2:
        return None
    return {"title": words[1], "employer": words[0],
            "location": words[2] if len(words) > 2 else None, "arrangement": None}


#: "Similar jobs to <title> at <employer>" - the header naming the posting
#: the seeker viewed.  The lead-in and the joining word are not the title.
_LEAD_IN_RE = re.compile(
    r"(similaires? à|similar to|vergelijkbaar met|ähnliche jobs wie)\s*$", re.IGNORECASE
)
_AT_WORDS = {"chez", "at", "bij", "bei"}
#: "Brussels (Hybrid)" - the work arrangement LinkedIn appends to the place.
_ARRANGEMENT_RE = re.compile(r"^(?P<place>.*?)\s*\((?P<mode>[^()]+)\)\s*$")


def _read_posting(
    texts: list[tuple[str, int]], pieces: list[tuple[str, str | None]]
) -> dict[str, Any] | None:
    """Title, employer, place and arrangement of one posting from its link texts.

    Three layouts, in the order they are recognised:

    * the title and an "Employer · Place" line both inside the posting link
      (LinkedIn's current alert);
    * "<lead-in> <title> chez|at <employer>" inside the link (the header that
      names the posting the seeker viewed);
    * only the title inside the link, with "Employer · Place" as plain text
      after it, up to the next link.
    """
    words = [t for t, _ in texts if t.lower() not in _BUTTON_TEXTS and not _LEAD_IN_RE.search(t)]
    employer = place = None
    title = None
    dotted = next((t for t in words if re.search(r"\s[·•]\s", t)), None)
    at_index = next((i for i, t in enumerate(words) if t.lower() in _AT_WORDS), None)
    if dotted is not None:
        employer, place = _employer_and_place([dotted])
        title = next((t for t in words if t != dotted and t.lower() not in _AT_WORDS), None)
    elif at_index is not None:
        title = words[at_index - 1] if at_index > 0 else None
        employer = words[at_index + 1] if at_index + 1 < len(words) else None
    else:
        candidates = [(t, i) for t, i in texts if t in words]
        if candidates:
            title, title_index = max(candidates, key=lambda c: len(c[0]))
            following: list[str] = []
            for text, href in pieces[title_index + 1:title_index + 4]:
                if href:
                    break
                following.append(text)
            employer, place = _employer_and_place(following)
    if not title:
        return None

    arrangement = None
    match = _ARRANGEMENT_RE.match(place or "")
    if match:
        place, arrangement = match.group("place") or None, match.group("mode")
    return {"title": title, "employer": employer, "location": place,
            "arrangement": arrangement}


#: How many plain texts after a linked title can still belong to its card: the
#: employer, the place, a badge ("Topjob") and a contract type.  A salary range
#: follows those in several pieces and is not read - the alert shows it as a
#: rounded "De ... à ... par mois", not as the posting's figure.
_LEAD_CARD_DETAILS = 4


def _read_lead_card(title: str, details: list[str]) -> dict[str, Any] | None:
    """One Jobat card: its linked title plus the plain texts under it.

    The employer and the place are rendered as ``<strong>Employer</strong> |
    Place``, which flattens either to two pieces (``"Employer"``, ``"| Place"``)
    or, if the bold is ever dropped, to one (``"Employer | Place"``).  Both are
    read here: the separator is the evidence, not the piece boundary.  A card
    with no separated place is not a posting card - that is what keeps the
    footer's ``Privacy policy | Contact`` row and the "Afficher tous les jobs"
    button out - so it returns ``None`` rather than a half-filled row.
    """
    if not title or title.lower() in _BUTTON_TEXTS:
        return None
    for position, detail in enumerate(details):
        employer, separator, place = detail.partition("|")
        if not separator or not place.strip():
            continue
        # "| Place" on its own line: the employer is what came before it.
        name = employer.strip() or " ".join(details[:position]).strip()
        return {"title": title, "employer": name or None,
                "location": place.strip(), "arrangement": None}
    return None


def _lead_cards(pieces: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    """The postings a link-less alert lists, read from the shape of each card.

    Jobat's alert routes every link - postings, navigation and footer alike -
    through one opaque redirector, so the href cannot say which anchor is a
    posting and there is no id to key on.  The card's shape is the only
    evidence left: an anchor is a posting when the plain texts that follow it,
    up to the next link, name an employer and a place.  Navigation ("19
    nouveaux jobs", "Afficher tous les jobs", "Modifier", the footer) never has
    that shape, and neither would a rewritten layout - which is what keeps
    ``alerts_without_postings`` an alarm rather than noise.

    ``posting_id`` and ``source_url`` are empty by construction: the alert
    states no id and no address, and inventing either (the redirector URL, a
    hash of the title) would put something in the row that the mail does not
    say, and that something would then be followed or deduplicated on.
    """
    cards: list[dict[str, Any]] = []
    index = 0
    while index < len(pieces):
        text, href = pieces[index]
        index += 1
        if href is None:
            continue
        # An anchor can hold several text pieces; they are all one title.
        titles = [text]
        while index < len(pieces) and pieces[index][1] == href:
            titles.append(pieces[index][0])
            index += 1
        details: list[str] = []
        while (index < len(pieces) and pieces[index][1] is None
               and len(details) < _LEAD_CARD_DETAILS):
            details.append(pieces[index][0])
            index += 1
        card = _read_lead_card(" ".join(titles), details)
        if card is not None:
            cards.append({"posting_id": None, "source_url": None, **card})
    return cards


def to_records(
    postings: list[dict[str, Any]], source: AlertSource, received_at: str
) -> list[NormalisedRecord]:
    """Map alert postings onto ``vacancy`` columns, provenance included.

    A lead-only source gets two fields differently, and both are deliberate.
    ``source_url`` stays empty: the alert has no posting address, and storing
    the redirector in its place would hand a Jobat URL to every part of the
    system that follows ``source_url`` - the apply route, the refresh pass -
    which is exactly the request this source exists to avoid.  And the channel
    says :data:`LEAD_CHANNEL` rather than the ``url`` that an empty URL would
    otherwise produce, so a row the seeker has to chase herself never looks
    like one with a broken link.  ``posted_at`` is the alert's own date, which
    the dedup key needs: without a date two different openings sharing a title,
    an employer and a place would have only the posting URL to tell them apart,
    and here there is none (see :func:`pipeline.dedup.vacancy_dedup_key`).
    """
    records: list[NormalisedRecord] = []
    for posting in postings:
        url = posting.get("source_url") or None
        if source.is_lead_only:
            channel, target = LEAD_CHANNEL, ""
            note = (f" (from a {source.label} e-mail, which carries no link to the "
                    "posting: look the offer up on the board itself)")
        else:
            channel, target = application_route(url or "", "")
            note = f" (from a {source.label} e-mail)"
        records.append(
            NormalisedRecord(
                entity_type="vacancy",
                data={
                    "company_name_raw": posting.get("employer"),
                    "title": posting["title"],
                    "description": " - ".join(
                        p for p in (posting["title"], posting.get("employer"),
                                    posting.get("location")) if p
                    ) + note,
                    "location": posting.get("location"),
                    "country": country_from_location(posting.get("location") or "", ""),
                    "work_arrangement": normalise_work_arrangement(posting.get("arrangement")),
                    "posted_at": received_at or None,
                    "application_channel": channel,
                    "application_target": target,
                    "source_url": url,
                },
                confidence=ALERT_CONFIDENCE,
                provenance={"alert_source": source.key},
            )
        )
    return records


def _gmail_query(cursor: str | None) -> str:
    if not cursor:
        return f"{GMAIL_QUERY} {FIRST_PASS_WINDOW}"
    # ``after:`` is whole-second and inclusive; a minute of overlap is free
    # because a re-read alert only merges.
    epoch = int(datetime.fromisoformat(cursor).timestamp()) - 60
    return f"{GMAIL_QUERY} after:{epoch}"


def _live_campaigns(job_seeker_id: str) -> list[str]:
    return [
        str(c["id"]) for c in campaign_repo.list_campaigns(job_seeker_id)
        if c.get("status") not in ("cancelled", "failed")
    ]


class MessageGone(Exception):
    """The message is no longer in the mailbox.

    A typed exception rather than a string the caller has to interpret. The
    decision it carries - may the cursor move past this message? - loses mail
    when it is wrong, so it is made once, where the server's answer is still in
    hand, instead of being re-derived from a formatted message later.

    Anything else raised from a fetch means "I could not read it", and the
    caller must fail the pass so the window is retried.
    """


#: IMAP response codes that make a `NO` transient rather than final. RFC 5530
#: wording plus the two Yahoo and Gmail send back under load. A `NO` carrying
#: one of these is the mailbox refusing us right now, not a missing message.
_IMAP_TRANSIENT_CODE = re.compile(
    r"\[(?:UNAVAILABLE|SERVERBUG|LIMIT|OVERQUOTA|INUSE|TRYCREATE|"
    r"AUTHENTICATIONFAILED|AUTHORIZATIONFAILED|EXPIRED|PRIVACYREQUIRED|"
    r"CONTACTADMIN|NOPERM|CANNOT)\]",
    re.IGNORECASE,
)


def _imap_detail(payload: object) -> str:
    """The server's own words from an imaplib payload, for the exception text.

    imaplib hands back a list whose entries are bytes, tuples or None. Keeping
    the text is the whole point: it is what distinguishes a deleted message from
    a busy server, and an earlier version discarded it.
    """
    if not isinstance(payload, (list, tuple)):
        return ""
    parts: list[str] = []
    for item in payload:
        if isinstance(item, bytes):
            parts.append(item.decode("utf-8", "replace"))
        elif isinstance(item, tuple):
            parts.extend(
                p.decode("utf-8", "replace") for p in item if isinstance(p, bytes)
            )
    return " ".join(parts).strip()


def ingest_account(account: dict, *, backend: Any = None) -> dict[str, Any]:
    """Read the new alert e-mails of one connected mailbox and feed their postings."""
    from dreamjob.mail.gmail import GmailBackend  # noqa: PLC0415 - optional OAuth stack

    seeker_id = str(account["job_seeker_id"])
    mailbox = backend or GmailBackend(account)
    cursor_key = f"{SETTING_CURSOR_PREFIX}{account['id']}"
    started = utcnow()
    cursor = admin_repo.get_setting(cursor_key)
    if hasattr(mailbox, "alert_ids"):
        ids = mailbox.alert_ids(cursor, MAX_MESSAGES)
    else:
        ids = mailbox.list_message_ids(_gmail_query(cursor), max_results=MAX_MESSAGES)

    report = {"account_id": account["id"], "alerts": 0, "alerts_without_postings": 0,
              "alerts_vanished": 0, "postings": 0, "new_vacancies": 0,
              "opportunities_added": 0, "notifications": 0}
    campaigns = _live_campaigns(seeker_id)
    for gmail_id in ids:
        # A message can disappear between the listing and the fetch, and here
        # that is the normal case rather than an edge one: the seeker is told she
        # may clear alerts once they are collected, and the listing is a snapshot
        # taken seconds earlier. IMAP answers NO for a uid that is gone (Yahoo:
        # "[CLIENTBUG] FETCH Bad sequence in the command"), and raising aborted
        # the whole pass - so deleting one already-read alert cost every alert
        # queued behind it. Counted and skipped: a pass that misses one mail is
        # still a pass, and the counter keeps it from being silent.
        try:
            raw = mailbox.fetch_raw(gmail_id)
        except MessageGone as exc:
            report["alerts_vanished"] += 1
            log.info("Alert %r is no longer in the mailbox; skipping it (%s)",
                     gmail_id, exc)
            continue
        # Anything that is not MessageGone means "I could not read it": a
        # revoked token, an exhausted quota, a dropped socket, a busy server.
        # It propagates on purpose, so `ingest_all` counts an error AND
        # `set_setting(cursor_key, started)` below is never reached - the next
        # pass then retries the same window. Swallowing these was worse than the
        # crash it replaced: the pass reported success having read nothing while
        # the cursor moved past every message in the window, which on Gmail
        # (second-precision cursor) loses them for good.
        message = email.message_from_bytes(raw, policy=email.policy.default)
        source = source_for(_first_address(message.get("From"))[0])
        if source is None:
            continue
        report["alerts"] += 1
        postings = parse_alert(message, source)
        if not postings:
            report["alerts_without_postings"] += 1
            continue
        report["postings"] += len(postings)
        writer = knowledge_base.KnowledgeBaseWriter(adapter_key=source.key)
        created = [
            o.entity_id for o in writer.write_many(to_records(postings, source, _received_at(message)))
            if o.entity_type == "vacancy" and o.created
        ]
        for row in (kb_repo.get_vacancy(vid) for vid in created):
            if not row:
                continue
            report["new_vacancies"] += 1
            company = {"company_id": row.get("company_id"),
                       "company_name": row.get("company_name_raw")}
            for campaign_id in campaigns:
                added = watchlist.add_to_ranked_list(seeker_id, campaign_id, [row])
                report["opportunities_added"] += len(added)
                report["notifications"] += notify_added(seeker_id, company, added)

    admin_repo.set_setting(cursor_key, started)
    return report


class ImapAlertMailbox:
    """A non-Gmail mailbox (Yahoo) read over IMAP with an app password.

    It offers the two calls :func:`ingest_account` needs - ``alert_ids`` in
    place of a Gmail search, and ``fetch_raw`` - so the parsing, the writer and
    the notifications are the same code as for Gmail.  IMAP ``SINCE`` has day
    precision, so a pass re-reads the cursor's whole day; that is harmless
    because a re-read alert only merges.  The mailbox is opened read-only.
    """

    def __init__(self, host: str, user: str, password: str, *, port: int = 993):
        self.host, self.user, self.password, self.port = host, user, password, port
        self._client: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> ImapAlertMailbox:
        self._client = imaplib.IMAP4_SSL(self.host, self.port)
        self._client.login(self.user, self.password)
        self._client.select("INBOX", readonly=True)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            try:
                self._client.logout()
            except Exception:  # noqa: BLE001 - closing must not mask the pass result
                log.debug("IMAP logout failed", exc_info=True)
            self._client = None

    def alert_ids(self, cursor: str | None, limit: int) -> list[bytes]:
        """UIDs of alert-sender messages since the cursor's day (or the first window)."""
        assert self._client is not None, "use ImapAlertMailbox as a context manager"
        since = datetime.fromisoformat(cursor) if cursor else datetime.now() - timedelta(days=14)
        criteria = ["SINCE", since.strftime("%d-%b-%Y")]
        senders = [s.sender_domain for s in ALERT_SOURCES]
        # IMAP OR takes two keys: fold "FROM a OR FROM b ..." into nested ORs.
        query: list[str] = ["FROM", f'"{senders[-1]}"']
        for domain in reversed(senders[:-1]):
            query = ["OR", "FROM", f'"{domain}"', *query]
        typ, data = self._client.uid("SEARCH", None, *query, *criteria)
        if typ != "OK":
            raise RuntimeError(f"IMAP search failed: {typ}")
        return (data[0] or b"").split()[-limit:]

    def fetch_raw(self, uid: bytes) -> bytes:
        """The raw message, or a `MessageGone` / `RuntimeError` saying which.

        The distinction is made HERE, by a typed exception, rather than left to
        the caller to recover from the string. An earlier version raised
        `RuntimeError(f"...failed: {typ}")` and threw the server's explanatory
        text away, so `NO [UNAVAILABLE] Server busy` and `NO [TRYCREATE]` arrived
        at the caller as the same four characters as a genuinely deleted
        message - and the caller skipped them and moved the cursor past them.
        That lost mail for a transient reason. The server's own words are the
        only thing that tells the two apart, so they must survive the raise.

        Two shapes mean "not there any more":
        - `NO` with no response code (or `[CLIENTBUG]`, which is what Yahoo
          answers for a vanished uid);
        - `OK` with no untagged FETCH response, which is what RFC 3501 section
          6.4.8 specifies and what `imaplib` returns as `("OK", [None])`. The
          earlier version re-raised on this one, i.e. it aborted the whole pass
          on the standard case it was written to survive.
        """
        assert self._client is not None, "use ImapAlertMailbox as a context manager"
        typ, payload = self._client.uid("FETCH", uid, "(RFC822)")
        detail = _imap_detail(payload)
        if typ == "OK" and (not payload or not isinstance(payload[0], tuple)):
            # RFC 3501: a FETCH of a uid that no longer exists succeeds silently.
            raise MessageGone(f"IMAP fetch of {uid!r}: OK with no message ({detail})")
        if typ != "OK" or not isinstance(payload[0], tuple):
            if typ == "NO" and not _IMAP_TRANSIENT_CODE.search(detail):
                raise MessageGone(f"IMAP fetch of {uid!r} failed: NO {detail}")
            raise RuntimeError(f"IMAP fetch of {uid!r} failed: {typ} {detail}")
        return payload[0][1]


def _ingest_imap(totals: dict[str, Any]) -> None:
    """The configured IMAP mailbox, if any, for the account that owns it."""
    settings = get_settings()
    user, password = settings.alerts_imap_user.strip(), settings.alerts_imap_password
    if not user or not password:
        return
    owner = settings.alerts_imap_owner.strip() or user
    seeker = seekers_repo.get_seeker_by_email(owner)
    if seeker is None:
        log.warning("Job alerts: no account logs in as %s; the IMAP mailbox is skipped", owner)
        totals["errors"] += 1
        return
    totals["mailboxes"] += 1
    with ImapAlertMailbox(settings.alerts_imap_host, user, password) as mailbox:
        result = ingest_account({"id": f"imap:{user}", "job_seeker_id": seeker["id"]},
                                backend=mailbox)
    for key in ("alerts", "alerts_without_postings", "alerts_vanished",
                "postings", "new_vacancies", "opportunities_added",
                "notifications"):
        totals[key] += int(result.get(key) or 0)


def ingest_all() -> dict[str, Any]:
    """Every connected mailbox; one failing mailbox never stops the others."""
    # alerts_without_postings and postings are aggregated too: the module
    # docstring promises that a changed alert layout "shows up as
    # alerts_without_postings in the pass report rather than as silence", and
    # the scheduler logs THESE totals - so leaving them out of the roll-up made
    # that alarm invisible in production, which is the only place it matters.
    totals: dict[str, Any] = {"mailboxes": 0, "errors": 0, "alerts": 0,
                              "alerts_without_postings": 0, "alerts_vanished": 0,
                              "postings": 0, "new_vacancies": 0,
                              "opportunities_added": 0, "notifications": 0}
    for account in dispatch_repo.accounts_to_poll("gmail_oauth"):
        totals["mailboxes"] += 1
        try:
            result = ingest_account(account)
        except Exception:  # noqa: BLE001 - a failing mailbox is counted, not raised
            log.exception("Job-alert pass failed for mailbox %s", account.get("id"))
            totals["errors"] += 1
            continue
        for key in ("alerts", "alerts_without_postings", "alerts_vanished",
                    "postings", "new_vacancies", "opportunities_added",
                    "notifications"):
            totals[key] += int(result.get(key) or 0)
    try:
        _ingest_imap(totals)
    except Exception:  # noqa: BLE001 - a failing mailbox is counted, not raised
        log.exception("Job-alert pass failed for the IMAP mailbox")
        totals["errors"] += 1
    return totals
