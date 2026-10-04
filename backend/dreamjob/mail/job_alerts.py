"""Job-alert e-mails from the seeker's own mailbox, turned into vacancies (FR-261, FR-401).

LinkedIn and ictjob.be both forbid automated collection of their sites, and
both offer what the seeker actually needs instead: job-alert e-mails.  Reading
those e-mails in the seeker's own inbox is ordinary use of a service the seeker
subscribed to, so this module is the lawful route to those two sources:

*It never visits linkedin.com or ictjob.be.*  It reads messages already
delivered to a mailbox the seeker connected - Gmail with ``gmail.readonly``,
the scope the mail slice already requests, or another provider such as Yahoo
over read-only IMAP with an app password (:class:`ImapAlertMailbox`) - and
extracts each posting's title, employer, location
and link from the alert's own HTML, and stores the link as ``source_url`` for
the seeker to open by hand.

*Only alert senders are read.*  The Gmail search names the alert senders, and
every fetched message is checked again against :data:`ALERT_SOURCES` before it
is parsed, so a personal e-mail is never turned into a vacancy.

*Deterministic, no model.*  An alert is a short list of links with a title and
an "Employer · Location" line under each; a link pattern per source picks the
postings out.  Nothing from the mailbox is sent to an LLM.

*Same path as every other new posting.*  Records go through the
knowledge-base writer, only rows it *created* count as new (a re-sent alert is
a merge), and new rows reach each live campaign through
:func:`watchlist.add_to_ranked_list` - directives filter them, scoring ranks
them - with the ``vacancy:<id>`` notification the refresh pass uses.

The link patterns follow the alert layouts as published; the ictjob.be one in
particular should be confirmed against a real alert, and a layout change shows
up as ``alerts_without_postings`` in the pass report rather than as silence.
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
    #: Matches a posting link and captures its id.
    link_re: re.Pattern[str]
    #: The canonical posting URL built from ``{id}`` (and the link's ``{host}``),
    #: or ``None`` to keep the matched URL without its tracking query.
    canonical: str | None
    #: ``title_first`` (LinkedIn, ictjob) or ``employer_first`` (Glassdoor:
    #: employer, rating, title, place inside the posting link).
    layout: str = "title_first"


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
)

#: Gmail search for the alert senders; ``after:`` or ``newer_than:`` is added.
GMAIL_QUERY = (
    "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com "
    "OR jobs-listings@linkedin.com OR ictjob.be OR noreply@glassdoor.com)"
)

#: How far back the first pass of a newly connected mailbox looks.
FIRST_PASS_WINDOW = "newer_than:14d"
#: Alerts read per mailbox per pass.
MAX_MESSAGES = 50
#: An alert snippet is a title, an employer and a place - no description.
ALERT_CONFIDENCE = 0.6
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


def _posting_url(source: AlertSource, href: str, posting_id: str) -> str:
    if source.canonical:
        return source.canonical.format(id=posting_id, host=urlparse(href).netloc)
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
    """
    parser = _LinkText()
    parser.feed(_html_of(message))
    pieces = parser.pieces

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


def to_records(
    postings: list[dict[str, Any]], source: AlertSource, received_at: str
) -> list[NormalisedRecord]:
    """Map alert postings onto ``vacancy`` columns, provenance included."""
    records: list[NormalisedRecord] = []
    for posting in postings:
        channel, target = application_route(posting["source_url"], "")
        records.append(
            NormalisedRecord(
                entity_type="vacancy",
                data={
                    "company_name_raw": posting.get("employer"),
                    "title": posting["title"],
                    "description": " - ".join(
                        p for p in (posting["title"], posting.get("employer"),
                                    posting.get("location")) if p
                    ) + f" (from a {source.label} e-mail)",
                    "location": posting.get("location"),
                    "country": country_from_location(posting.get("location") or "", ""),
                    "work_arrangement": normalise_work_arrangement(posting.get("arrangement")),
                    "posted_at": received_at or None,
                    "application_channel": channel,
                    "application_target": target,
                    "source_url": posting["source_url"],
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
              "postings": 0, "new_vacancies": 0, "opportunities_added": 0, "notifications": 0}
    campaigns = _live_campaigns(seeker_id)
    for gmail_id in ids:
        message = email.message_from_bytes(mailbox.fetch_raw(gmail_id), policy=email.policy.default)
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
        assert self._client is not None, "use ImapAlertMailbox as a context manager"
        typ, payload = self._client.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not payload or not isinstance(payload[0], tuple):
            raise RuntimeError(f"IMAP fetch of {uid!r} failed: {typ}")
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
    for key in ("alerts", "new_vacancies", "opportunities_added", "notifications"):
        totals[key] += int(result.get(key) or 0)


def ingest_all() -> dict[str, Any]:
    """Every connected mailbox; one failing mailbox never stops the others."""
    totals: dict[str, Any] = {"mailboxes": 0, "errors": 0, "alerts": 0, "new_vacancies": 0,
                              "opportunities_added": 0, "notifications": 0}
    for account in dispatch_repo.accounts_to_poll("gmail_oauth"):
        totals["mailboxes"] += 1
        try:
            result = ingest_account(account)
        except Exception:  # noqa: BLE001 - a failing mailbox is counted, not raised
            log.exception("Job-alert pass failed for mailbox %s", account.get("id"))
            totals["errors"] += 1
            continue
        for key in ("alerts", "new_vacancies", "opportunities_added", "notifications"):
            totals[key] += int(result.get(key) or 0)
    try:
        _ingest_imap(totals)
    except Exception:  # noqa: BLE001 - a failing mailbox is counted, not raised
        log.exception("Job-alert pass failed for the IMAP mailbox")
        totals["errors"] += 1
    return totals
