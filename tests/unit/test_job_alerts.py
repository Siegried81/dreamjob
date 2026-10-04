"""Job-alert e-mails turned into vacancies (``mail/job_alerts.py``).

The properties pinned down here:

* only the alert senders are recognised - a look-alike domain or a personal
  e-mail that merely contains a LinkedIn link is never parsed;
* a LinkedIn and an ictjob.be alert yield title, employer, place and a clean
  posting URL, and button links ("View job") never become titles;
* a pass feeds new postings to the seeker's live campaign and notifies once;
  the same alert read again creates nothing (a merge is not news).

No network and no Gmail: the mailbox is a fake with ``list_message_ids`` and
``fetch_raw``, the two calls the real ``GmailBackend`` offers.
"""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

from dreamjob.db.connection import query_all
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.mail import job_alerts
from test_vacancy_refresh import seed_campaign

LINKEDIN_HTML = """
<html><head><style>.x{color:red}</style></head><body>
<table>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/view/3901234567/?trackingId=abc&refId=x">
   Senior Data Engineer</a></td></tr>
 <tr><td>Northwind · Brussels, Belgium</td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/view/3901234567/?trk=btn">View job</a></td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/view/3907654321/">Analytics Engineer</a></td></tr>
 <tr><td>Contoso</td></tr><tr><td>Ghent, Flanders, Belgium</td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/search/?keywords=data">See all jobs</a></td></tr>
</table></body></html>
"""

ICTJOB_HTML = """
<html><body>
<a href="https://www.ictjob.be/en/it-jobs/data-engineer-acme-1234567?utm_source=alert">Data Engineer</a>
<p>Acme - Leuven</p>
</body></html>
"""


def _message(sender: str, html: str, subject: str = "New jobs for you") -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "seeker@example.test"
    message["Subject"] = subject
    message["Date"] = "Sun, 04 Oct 2026 09:00:00 +0000"
    message.set_content("Open this e-mail in HTML.")
    message.add_alternative(html, subtype="html")
    return message


def _parsed(message: EmailMessage) -> EmailMessage:
    return email.message_from_bytes(bytes(message), policy=email.policy.default)


class FakeMailbox:
    """The two ``GmailBackend`` calls the alert pass uses, over fixed messages."""

    def __init__(self, messages: dict[str, EmailMessage]):
        self.messages = messages
        self.queries: list[str] = []

    def list_message_ids(self, query: str, *, max_results: int = 50) -> list[str]:
        self.queries.append(query)
        return list(self.messages)[:max_results]

    def fetch_raw(self, gmail_id: str) -> bytes:
        return bytes(self.messages[gmail_id])


# ---------------------------------------------------------------------------
# Who counts as an alert sender
# ---------------------------------------------------------------------------


def test_only_alert_senders_are_recognised() -> None:
    assert job_alerts.source_for("jobalerts-noreply@linkedin.com").key == "alert.linkedin"
    assert job_alerts.source_for("jobs-noreply@linkedin.com").key == "alert.linkedin"
    assert job_alerts.source_for("alerts@ictjob.be").key == "alert.ictjob"
    # LinkedIn's other mail (messages, invitations) is not a job alert.
    assert job_alerts.source_for("messages-noreply@linkedin.com") is None
    assert job_alerts.source_for("jobalerts-noreply@linkedin.com.evil.example") is None
    assert job_alerts.source_for("friend@gmail.com") is None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_linkedin_alert_yields_each_posting_once() -> None:
    message = _parsed(_message("LinkedIn <jobalerts-noreply@linkedin.com>", LINKEDIN_HTML))
    postings = job_alerts.parse_alert(message, job_alerts.source_for("jobalerts-noreply@linkedin.com"))

    assert [p["title"] for p in postings] == ["Senior Data Engineer", "Analytics Engineer"]
    first, second = postings
    assert (first["employer"], first["location"]) == ("Northwind", "Brussels, Belgium")
    assert (second["employer"], second["location"]) == ("Contoso", "Ghent, Flanders, Belgium")
    # Tracking parameters are dropped: the canonical posting URL is stored.
    assert first["source_url"] == "https://www.linkedin.com/jobs/view/3901234567/"


def test_an_ictjob_alert_keeps_the_posting_url_without_tracking() -> None:
    message = _parsed(_message("ictjob.be <alerts@ictjob.be>", ICTJOB_HTML))
    postings = job_alerts.parse_alert(message, job_alerts.source_for("alerts@ictjob.be"))

    assert len(postings) == 1
    assert postings[0]["title"] == "Data Engineer"
    assert (postings[0]["employer"], postings[0]["location"]) == ("Acme", "Leuven")
    assert postings[0]["source_url"] == (
        "https://www.ictjob.be/en/it-jobs/data-engineer-acme-1234567"
    )


# ---------------------------------------------------------------------------
# A pass over a mailbox
# ---------------------------------------------------------------------------


def _account(seeker_id: str) -> dict:
    return {"id": f"acct-{seeker_id}", "job_seeker_id": seeker_id, "backend": "gmail_oauth"}


def test_a_pass_feeds_the_live_campaign_and_a_repeat_creates_nothing() -> None:
    campaign = seed_campaign("alerts-ann@example.test")
    seeker_id = campaign["seeker_id"]
    mailbox = FakeMailbox({
        "m1": _message("LinkedIn <jobalerts-noreply@linkedin.com>", LINKEDIN_HTML),
        # A personal e-mail sharing a LinkedIn job link is not an alert.
        "m2": _message("Friend <friend@gmail.com>",
                       '<a href="https://www.linkedin.com/jobs/view/3999999999/">Look!</a>'),
    })

    report = job_alerts.ingest_account(_account(seeker_id), backend=mailbox)

    assert report["alerts"] == 1 and report["postings"] == 2
    assert report["new_vacancies"] == 2
    assert report["opportunities_added"] == 2 and report["notifications"] == 2
    titles = {r["title"] for r in query_all(
        "SELECT title FROM opportunity WHERE campaign_id = ?", (campaign["campaign_id"],))}
    assert {"Senior Data Engineer", "Analytics Engineer"} <= titles
    assert "Look!" not in titles
    notes = repo.list_notifications(seeker_id, kind="new_vacancy")
    assert len(notes) == 2
    # The first pass looks back a fortnight; the cursor makes the next incremental.
    assert "newer_than:14d" in mailbox.queries[0]
    assert admin_repo.get_setting(f"{job_alerts.SETTING_CURSOR_PREFIX}acct-{seeker_id}")

    again = job_alerts.ingest_account(_account(seeker_id), backend=mailbox)

    assert again["new_vacancies"] == 0 and again["notifications"] == 0
    assert "after:" in mailbox.queries[1]
    assert len(repo.list_notifications(seeker_id, kind="new_vacancy")) == 2


def test_an_alert_whose_layout_changed_is_reported_not_silent() -> None:
    campaign = seed_campaign("alerts-bob@example.test")
    mailbox = FakeMailbox({
        "m1": _message("LinkedIn <jobalerts-noreply@linkedin.com>", "<p>No links any more</p>"),
    })

    report = job_alerts.ingest_account(_account(campaign["seeker_id"]), backend=mailbox)

    assert report["alerts"] == 1 and report["alerts_without_postings"] == 1
    assert report["new_vacancies"] == 0


# ---------------------------------------------------------------------------
# A non-Gmail mailbox over IMAP (Yahoo)
# ---------------------------------------------------------------------------


class FakeImap:
    """``imaplib.IMAP4_SSL`` over fixed messages; records what was asked."""

    instances: list[FakeImap] = []

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.logged_in: tuple[str, str] | None = None
        self.selected: tuple[str, bool] | None = None
        self.searches: list[tuple] = []
        self.messages: dict[bytes, bytes] = {}
        self.closed = False
        FakeImap.instances.append(self)

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def select(self, folder: str, readonly: bool = False) -> None:
        self.selected = (folder, readonly)

    def uid(self, command: str, *args):
        if command == "SEARCH":
            self.searches.append(args)
            return "OK", [b" ".join(self.messages)]
        return "OK", [(b"1 (RFC822)", self.messages[args[0]])]

    def logout(self) -> None:
        self.closed = True


def test_the_imap_search_names_every_alert_sender_and_a_day(monkeypatch) -> None:
    monkeypatch.setattr(job_alerts.imaplib, "IMAP4_SSL", FakeImap)
    with job_alerts.ImapAlertMailbox("imap.mail.yahoo.com", "me@yahoo.fr", "app-pw") as box:
        box._client.messages = {b"7": b"", b"8": b"", b"9": b""}
        ids = box.alert_ids("2026-10-04T09:00:00+00:00", 2)
    imap = FakeImap.instances[-1]

    assert ids == [b"8", b"9"]                     # the newest, within the limit
    assert imap.logged_in == ("me@yahoo.fr", "app-pw")
    assert imap.selected == ("INBOX", True)        # read-only
    assert imap.searches[0][1:] == (
        "OR", "FROM", '"linkedin.com"', "FROM", '"ictjob.be"', "SINCE", "04-Oct-2026",
    )
    assert imap.closed


def test_a_yahoo_mailbox_feeds_the_account_that_logs_in_with_it(monkeypatch) -> None:
    from dreamjob.config import get_settings

    campaign = seed_campaign("alerts-yahoo@example.test")
    monkeypatch.setattr(job_alerts, "get_settings", lambda: get_settings().model_copy(update={
        "alerts_imap_user": "alerts-yahoo@example.test", "alerts_imap_password": "app-pw",
    }))
    # Own postings: the scratch database is shared, and a posting another test
    # already stored (same id, or same title at the same employer) would merge
    # here instead of being new.
    html = (LINKEDIN_HTML.replace("3901234567", "4801234567").replace("3907654321", "4807654321")
            .replace("Senior Data Engineer", "ML Platform Engineer")
            .replace("Analytics Engineer", "BI Developer")
            .replace("Northwind", "Fabrikam").replace("Contoso", "Tailspin"))
    message = _message("LinkedIn <jobalerts-noreply@linkedin.com>", html)

    class LoadedImap(FakeImap):
        def __init__(self, host: str, port: int) -> None:
            super().__init__(host, port)
            self.messages = {b"1": bytes(message)}

    monkeypatch.setattr(job_alerts.imaplib, "IMAP4_SSL", LoadedImap)
    monkeypatch.setattr(job_alerts.dispatch_repo, "accounts_to_poll", lambda _b: [])

    totals = job_alerts.ingest_all()

    assert totals["mailboxes"] == 1 and totals["errors"] == 0
    assert totals["new_vacancies"] == 2
    assert len(repo.list_notifications(campaign["seeker_id"], kind="new_vacancy")) == 2


def test_an_imap_user_with_no_account_is_reported(monkeypatch) -> None:
    from dreamjob.config import get_settings

    monkeypatch.setattr(job_alerts, "get_settings", lambda: get_settings().model_copy(update={
        "alerts_imap_user": "nobody@yahoo.fr", "alerts_imap_password": "app-pw",
    }))
    monkeypatch.setattr(job_alerts.dispatch_repo, "accounts_to_poll", lambda _b: [])

    totals = job_alerts.ingest_all()

    assert totals["mailboxes"] == 0 and totals["errors"] == 1


# The layout LinkedIn actually sends (checked on a delivered alert): title and
# "Employer · Place (Arrangement)" both inside the posting link, a header
# naming the viewed posting, and search links that are not postings.
LINKEDIN_CURRENT_HTML = """
<html><body>
<a href="https://www.linkedin.com/comm/jobs/view/5100000001/?trk=a">
  <span>Offres d’emploi similaires à</span><span>Platform Engineer</span>
  <span>chez</span><span>Globex</span></a>
<a href="https://www.linkedin.com/comm/jobs/view/5100000002/?trk=b">
  <p>Data Engineer - Streaming</p><p>Initech · Bruxelles (Hybride)</p></a>
<a href="https://www.linkedin.com/comm/jobs/view/5100000003/?trk=c">
  <p>ML Engineer</p><p>Hooli · Gand (Sur site)</p></a>
<a href="https://www.linkedin.com/comm/jobs/view/5100000004/">
  <p>Python Developer</p><p>Umbrella · Région de Bruxelles-Capitale, Belgique</p></a>
<a href="https://www.linkedin.com/comm/jobs/search/?keywords=x">Voir toutes les offres d’emploi</a>
</body></html>
"""


def test_the_current_linkedin_layout_reads_employer_place_and_arrangement() -> None:
    message = _parsed(_message("LinkedIn <jobalerts-noreply@linkedin.com>", LINKEDIN_CURRENT_HTML))
    postings = job_alerts.parse_alert(message, job_alerts.ALERT_SOURCES[0])

    assert [(p["title"], p["employer"], p["location"], p["arrangement"]) for p in postings] == [
        ("Platform Engineer", "Globex", None, None),
        ("Data Engineer - Streaming", "Initech", "Bruxelles", "Hybride"),
        ("ML Engineer", "Hooli", "Gand", "Sur site"),
        ("Python Developer", "Umbrella", "Région de Bruxelles-Capitale, Belgique", None),
    ]
    records = job_alerts.to_records(postings, job_alerts.ALERT_SOURCES[0], "2026-10-04T09:00:00+00:00")
    assert [r.data["work_arrangement"] for r in records] == [None, "hybrid", "onsite", None]
