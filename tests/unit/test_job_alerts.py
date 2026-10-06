"""Job-alert e-mails turned into vacancies (``mail/job_alerts.py``).

The properties pinned down here:

* only the alert senders are recognised - a look-alike domain or a personal
  e-mail that merely contains a LinkedIn link is never parsed;
* a LinkedIn and an ictjob.be alert yield title, employer, place and a clean
  posting URL, and button links ("View job") never become titles;
* a Jobat alert, whose every link is one opaque redirector, yields title,
  employer and place with *no* URL - navigation is still kept out, the rows say
  in ``application_channel`` that the offer has to be looked up, and two
  distinct openings never collapse onto one row for want of a URL;
* a pass feeds new postings to the seeker's live campaign and notifies once;
  the same alert read again creates nothing (a merge is not news).

No network and no Gmail: the mailbox is a fake with ``list_message_ids`` and
``fetch_raw``, the two calls the real ``GmailBackend`` offers.  Nothing here
requests a jobat.be URL either: the Jobat fixture is the delivered alert's
markup, redirector hrefs included, parsed in process.
"""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

import pytest

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
        "OR", "FROM", '"linkedin.com"', "OR", "FROM", '"ictjob.be"',
        "OR", "FROM", '"glassdoor.com"', "FROM", '"jobat.be"',
        "SINCE", "04-Oct-2026",
    )
    assert imap.closed


def test_a_yahoo_mailbox_feeds_the_account_that_logs_in_with_it(monkeypatch) -> None:
    from dreamjob.config import get_settings

    campaign = seed_campaign("alerts-yahoo@example.test")
    monkeypatch.setattr(job_alerts, "get_settings", lambda: get_settings().model_copy(update={
        "alerts_imap_user": "alerts-yahoo@example.test", "alerts_imap_password": "app-pw",
        "alerts_imap_owner": "",
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
        "alerts_imap_owner": "",
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


# Glassdoor's layout (checked on a delivered alert): employer, star rating,
# title, place, then an optional salary estimate - all inside one link per
# posting whose query carries a stable jobListingId among tracking parameters.
GLASSDOOR_HTML = """
<html><body>
<a href="https://fr.glassdoor.be/Emploi/bruxelles-emplois-SRCH_IL.0,9.htm?x=1">recherchez plus</a>
<a href="https://fr.glassdoor.be/partner/jobListing.htm?pos=101&guid=abc&jobListingId=1010000000001&utm_source=x">
  <span>Initech</span><span>3.6 ★</span><span>Data Analyst</span><span>Bruxelles</span>
  <span>Candidature facile</span></a>
<a href="https://fr.glassdoor.be/partner/jobListing.htm?pos=102&jobListingId=1010000000002&cpc=Z">
  <span>Globex</span><span>5.0 ★</span><span>AI Engineer</span><span>Gand</span>
  <span>52 k € - 1 M €</span><span>(</span><span>Estimation de l'employeur</span><span>)</span></a>
<a href="https://fr.glassdoor.be/profile/unsubscribeEmail.htm?key=k">Se désabonner</a>
</body></html>
"""


def test_a_glassdoor_alert_reads_employer_first_cards() -> None:
    source = job_alerts.source_for("noreply@glassdoor.com")
    message = _parsed(_message("Glassdoor <noreply@glassdoor.com>", GLASSDOOR_HTML))

    postings = job_alerts.parse_alert(message, source)

    assert source.key == "alert.glassdoor"
    assert [(p["title"], p["employer"], p["location"]) for p in postings] == [
        ("Data Analyst", "Initech", "Bruxelles"),
        ("AI Engineer", "Globex", "Gand"),
    ]
    # Only the stable id survives; tracking parameters are dropped.
    assert postings[0]["source_url"] == (
        "https://fr.glassdoor.be/partner/jobListing.htm?jobListingId=1010000000001"
    )


def test_the_owner_setting_sends_a_mailbox_to_another_account(monkeypatch) -> None:
    from dreamjob.config import get_settings

    campaign = seed_campaign("alerts-owner@example.test")
    monkeypatch.setattr(job_alerts, "get_settings", lambda: get_settings().model_copy(update={
        "alerts_imap_user": "someone@yahoo.fr", "alerts_imap_password": "app-pw",
        "alerts_imap_owner": "alerts-owner@example.test",
    }))
    html = (LINKEDIN_HTML.replace("3901234567", "4901234567").replace("3907654321", "4907654321")
            .replace("Senior Data Engineer", "Data Platform Lead").replace("Northwind", "Vandelay")
            .replace("Analytics Engineer", "Insights Analyst").replace("Contoso", "Pendant"))
    message = _message("LinkedIn <jobalerts-noreply@linkedin.com>", html)

    class LoadedImap(FakeImap):
        def __init__(self, host: str, port: int) -> None:
            super().__init__(host, port)
            self.messages = {b"1": bytes(message)}

    monkeypatch.setattr(job_alerts.imaplib, "IMAP4_SSL", LoadedImap)
    monkeypatch.setattr(job_alerts.dispatch_repo, "accounts_to_poll", lambda _b: [])

    totals = job_alerts.ingest_all()

    assert totals["errors"] == 0 and totals["new_vacancies"] == 2
    assert len(repo.list_notifications(campaign["seeker_id"], kind="new_vacancy")) == 2


# ---------------------------------------------------------------------------
# Jobat: a lead detector, not a link harvester
# ---------------------------------------------------------------------------

# The layout Jobat actually sends (read off two delivered alerts, FR edition):
# every link - the "19 nouveaux jobs" header, each title, "Afficher tous les
# jobs", "Modifier" and the footer - is the same optiextension.dll redirector,
# so only the card's shape says which anchor is a posting: a linked title, the
# employer in bold, then "&nbsp;|&nbsp;Place", then badges and a contract type.
# The last two cards are the shapes that have to survive: a title carrying a
# pipe of its own, and an employer and place rendered as one piece.
_JOBAT_LINK = "https://interactief.jobat.be/optiext/optiextension.dll?ID="
JOBAT_HTML = f"""
<html><head><style>.x{{color:red}}</style></head><body>
<p><strong>Il y a <a href="{_JOBAT_LINK}blob0">19 nouveaux jobs</a> pour vous.</strong></p>
<table><tr><td>
  <span><a href="{_JOBAT_LINK}blob1">Senior Business Analyst &ndash; IT programme</a></span><br />
  <strong>Kingfisher It</strong>
  <span>&nbsp;|&nbsp;Dilbeek</span><br />
  <span>Dur&eacute;e ind&eacute;termin&eacute;e</span>
</td></tr></table>
<table><tr><td>
  <span><a href="{_JOBAT_LINK}blob2">Data engineer (VDAB)</a></span><br />
  <strong>Vlaanderen Connect</strong>
  <span>&nbsp;|&nbsp;Brabant Flamand</span><br />
  <span>Topjob</span><span>Dur&eacute;e d&eacute;termin&eacute;e</span>
</td></tr></table>
<a href="{_JOBAT_LINK}blob3">Afficher tous les jobs</a>
<table><tr><td>
  <span><a href="{_JOBAT_LINK}blob4">Real Estate Valuation Analyst | Finance, Data &amp; AI</a></span><br />
  <strong>Headcount</strong>
  <span>&nbsp;|&nbsp;Saint-Josse-Ten-Noode</span>
</td></tr></table>
<table><tr><td>
  <span><a href="{_JOBAT_LINK}blob5">Data Quality Expert</a></span><br />
  <span>Smals&nbsp;|&nbsp;Bruxelles</span>
</td></tr></table>
<p>Vos crit&egrave;res :</p><p>business analyst , Bruxelles</p>
<a href="{_JOBAT_LINK}blob6">Modifier</a>
<p>&copy; copyright 2026 - Jobat - Z1 Researchpark 110 - 1731 ZELLIK</p>
<a href="{_JOBAT_LINK}blob7">Privacy policy</a> | <a href="{_JOBAT_LINK}blob8">Contact</a>
| <a href="{_JOBAT_LINK}blob9">Gestion des courriels</a>
<p>Ce message a &eacute;t&eacute; envoy&eacute; &agrave;
<a href="{_JOBAT_LINK}blobA">seeker@example.test</a>.</p>
<p>Vous ne souhaitez plus recevoir cette alerte emploi ?
<a href="{_JOBAT_LINK}blobB">G&eacute;rez vos pr&eacute;f&eacute;rences ici</a>.</p>
</body></html>
"""


def test_a_jobat_alert_reads_cards_by_their_shape_and_keeps_navigation_out() -> None:
    source = job_alerts.source_for("jobat@jobs.jobat.be")
    message = _parsed(_message("Jobat <jobat@jobs.jobat.be>", JOBAT_HTML))

    postings = job_alerts.parse_alert(message, source)

    assert (source.key, source.is_lead_only) == ("alert.jobat", True)
    assert [(p["title"], p["employer"], p["location"]) for p in postings] == [
        ("Senior Business Analyst – IT programme", "Kingfisher It", "Dilbeek"),
        ("Data engineer (VDAB)", "Vlaanderen Connect", "Brabant Flamand"),
        # A pipe inside the title is part of the title; the separator that
        # matters is the one in the plain text under it.
        ("Real Estate Valuation Analyst | Finance, Data & AI", "Headcount",
         "Saint-Josse-Ten-Noode"),
        ("Data Quality Expert", "Smals", "Bruxelles"),
    ]
    # Nothing is invented for the missing link, not even the redirector.
    assert all(p["source_url"] is None and p["posting_id"] is None for p in postings)


def test_a_jobat_row_says_the_offer_has_to_be_looked_up() -> None:
    source = job_alerts.source_for("jobat@jobs.jobat.be")
    message = _parsed(_message("Jobat <jobat@jobs.jobat.be>", JOBAT_HTML))

    records = job_alerts.to_records(
        job_alerts.parse_alert(message, source), source, "2026-10-06T04:50:08+00:00"
    )

    first = records[0].data
    assert first["source_url"] is None
    assert first["application_channel"] == job_alerts.LEAD_CHANNEL == "manual_search"
    assert first["application_target"] == ""
    # The row carries the reason on its face, wherever the description is read.
    assert "carries no link to the posting" in first["description"]
    assert first["posted_at"] == "2026-10-06T04:50:08+00:00"
    assert records[0].provenance == {"alert_source": "alert.jobat"}


def test_a_link_less_alert_whose_layout_changed_is_reported_not_silent() -> None:
    """The counter has to stay an alarm: no card shape must mean no postings."""
    source = job_alerts.source_for("jobat@jobs.jobat.be")
    # The same redirector links, none of them in a card.
    html = (f'<html><body><a href="{_JOBAT_LINK}b1">19 nouveaux jobs</a>'
            f'<p>pour vous.</p><a href="{_JOBAT_LINK}b2">Afficher tous les jobs</a>'
            f'<p>Vos crit&egrave;res :</p><p>business analyst</p></body></html>')
    message = _parsed(_message("Jobat <jobat@jobs.jobat.be>", html))

    assert job_alerts.parse_alert(message, source) == []


def test_jobat_leads_with_no_url_still_key_apart_and_a_repeat_merges() -> None:
    """The dedup key without a URL: the alert's own date is what keeps it safe.

    The posting URL is only the key's discriminator when there is no posting
    date, so a dateless lead with no URL would have no discriminator at all.
    The alert's date supplies one: two openings at one employer must still key
    apart, and the same opening re-sent must still be one row.
    """
    from dreamjob.pipeline import dedup

    first = dedup.vacancy_dedup_key(
        "Data Quality Expert", "Smals", "Bruxelles", "2026-10-06T04:50:08+00:00", None)
    other = dedup.vacancy_dedup_key(
        "Senior Data Analyst", "Smals", "Bruxelles", "2026-10-06T04:50:08+00:00", None)
    next_day = dedup.vacancy_dedup_key(
        "Data Quality Expert", "Smals", "Bruxelles", "2026-10-07T04:50:08+00:00", None)
    next_week = dedup.vacancy_dedup_key(
        "Data Quality Expert", "Smals", "Bruxelles", "2026-10-14T04:50:08+00:00", None)

    assert first != other                       # two openings, two rows
    assert first == next_day                    # the same opening, re-sent
    # A new week keys differently, and the fuzzy pass is what catches it.
    assert first != next_week
    row = {"title": "Data Quality Expert", "company_name_raw": "Smals",
           "location": "Bruxelles", "posted_at": "2026-10-14T04:50:08+00:00"}
    held = {"id": "v1", "title": "Data Quality Expert", "company_name_raw": "Smals",
            "location": "Bruxelles", "posted_at": "2026-10-06T04:50:08+00:00"}
    matched, score = dedup.best_vacancy_match(row, [held])
    assert matched["id"] == "v1" and score >= dedup.VACANCY_MATCH_THRESHOLD
    different = {"id": "v2", "title": "Senior Data Analyst", "company_name_raw": "Smals",
                 "location": "Bruxelles", "posted_at": "2026-10-06T04:50:08+00:00"}
    assert dedup.best_vacancy_match(row, [different])[0] is None


def test_a_jobat_pass_feeds_leads_once() -> None:
    campaign = seed_campaign("alerts-jobat@example.test")
    seeker_id = campaign["seeker_id"]
    # Own titles and employers: the scratch database is shared, and a posting
    # another test stored under the same title would merge instead of be new.
    html = (JOBAT_HTML.replace("Senior Business Analyst &ndash; IT programme",
                               "Lead Reporting Analyst")
            .replace("Data engineer (VDAB)", "Streaming Data Engineer (KLM)")
            .replace("Real Estate Valuation Analyst | Finance, Data &amp; AI",
                     "Valuation Analyst | Finance &amp; AI")
            .replace("Data Quality Expert", "Reference Data Steward")
            .replace("Kingfisher It", "Zephyr It").replace("Vlaanderen Connect", "Connect Vl")
            .replace("Headcount", "Headwind").replace("Smals", "Smalls"))
    mailbox = FakeMailbox({"m1": _message("Jobat <jobat@jobs.jobat.be>", html)})

    report = job_alerts.ingest_account(_account(seeker_id), backend=mailbox)

    assert report["alerts"] == 1 and report["alerts_without_postings"] == 0
    assert report["postings"] == 4 and report["new_vacancies"] == 4
    assert report["opportunities_added"] == 4
    rows = query_all(
        "SELECT title, source_url, application_channel FROM vacancy "
        "WHERE source_adapter = 'alert.jobat' AND company_name_raw = 'Zephyr It'", ())
    assert [(r["source_url"], r["application_channel"]) for r in rows] == [
        (None, "manual_search")]

    again = job_alerts.ingest_account(_account(seeker_id), backend=mailbox)

    assert again["postings"] == 4 and again["new_vacancies"] == 0


def test_the_three_linked_senders_are_untouched_by_the_link_less_one() -> None:
    """The optional link pattern must not have moved anything for the others."""
    linkedin, ictjob, glassdoor, jobat = job_alerts.ALERT_SOURCES
    assert [s.key for s in (linkedin, ictjob, glassdoor)] == [
        "alert.linkedin", "alert.ictjob", "alert.glassdoor"]
    assert not any(s.is_lead_only for s in (linkedin, ictjob, glassdoor))
    assert jobat.is_lead_only and jobat.layout == "lead_cards"

    message = _parsed(_message("LinkedIn <jobalerts-noreply@linkedin.com>", LINKEDIN_HTML))
    records = job_alerts.to_records(
        job_alerts.parse_alert(message, linkedin), linkedin, "2026-10-04T09:00:00+00:00")

    assert records[0].confidence == job_alerts.ALERT_CONFIDENCE
    assert records[0].data == {
        "company_name_raw": "Northwind",
        "title": "Senior Data Engineer",
        "description": "Senior Data Engineer - Northwind - Brussels, Belgium "
                       "(from a LinkedIn job alert e-mail)",
        "location": "Brussels, Belgium",
        "country": "BE",
        "work_arrangement": None,
        "posted_at": "2026-10-04T09:00:00+00:00",
        "application_channel": "url",
        "application_target": "https://www.linkedin.com/jobs/view/3901234567/",
        "source_url": "https://www.linkedin.com/jobs/view/3901234567/",
    }


def test_a_vanished_message_is_skipped_not_fatal() -> None:
    """Deleting an already-collected alert must not cost the alerts queued behind
    it. The listing is a snapshot taken seconds before the fetch, and the seeker
    is explicitly told she may clear collected alerts - so a uid that is gone is
    the normal case here, not an edge one. IMAP answers NO for it (Yahoo:
    "[CLIENTBUG] FETCH Bad sequence in the command"), and raising used to abort
    the whole pass.

    Observed for real on 2026-10-06: uid 484936 had been deleted between the
    listing and the fetch, and the pass returned errors=1 having read no alerts.
    """
    campaign = seed_campaign("alerts-vanished@example.test")
    seeker_id = campaign["seeker_id"]

    class _PartlyGoneMailbox:
        def __init__(self) -> None:
            self.fetched: list[str] = []

        def list_message_ids(self, query, max_results=50):
            return ["gone", "good"]

        def fetch_raw(self, message_id):
            self.fetched.append(message_id)
            if message_id == "gone":
                raise job_alerts.MessageGone(
                    "IMAP fetch of b'484936' failed: NO [CLIENTBUG] Bad sequence"
                )
            return _message("LinkedIn <jobalerts-noreply@linkedin.com>",
                            LINKEDIN_HTML).as_bytes()

    mailbox = _PartlyGoneMailbox()
    report = job_alerts.ingest_account(_account(seeker_id), backend=mailbox)

    # Both were attempted, and the survivor was still parsed.
    assert mailbox.fetched == ["gone", "good"]
    assert report["alerts_vanished"] == 1
    assert report["alerts"] == 1
    assert report["postings"] == 2


def test_a_transient_fetch_failure_fails_the_pass_instead_of_skipping() -> None:
    """A revoked token is not a deleted message, and skipping it loses mail.

    The first version of the skip caught every exception. Measured in-process:
    three messages failing on `401 Unauthorized: invalid_grant` gave
    `alerts_vanished: 3`, `errors: 0`, and the cursor still advanced past all
    three - permanently, since Gmail's cursor has second precision. The pass has
    to fail so the cursor is never written and the window is retried.
    """
    campaign = seed_campaign("alerts-revoked@example.test")
    seeker_id = campaign["seeker_id"]

    class _RevokedMailbox:
        def list_message_ids(self, query, max_results=50):
            return ["a", "b"]

        def fetch_raw(self, message_id):
            raise RuntimeError("401 Unauthorized: invalid_grant")

    with pytest.raises(RuntimeError, match="invalid_grant"):
        job_alerts.ingest_account(_account(seeker_id), backend=_RevokedMailbox())


def test_imap_tells_a_vanished_message_from_a_busy_server() -> None:
    """The decision that loses mail when it is wrong, made where the server's
    own words are still in hand.

    An earlier version raised `RuntimeError(f"...failed: {typ}")` and discarded
    the response text, so `NO [UNAVAILABLE] Server busy` reached the caller as
    the same string as a deleted message: it was skipped, counted as vanished,
    and the cursor moved past it. A transient refusal cost real alerts.

    `OK` with no untagged FETCH response is the RFC 3501 6.4.8 shape for a uid
    that no longer exists, and `imaplib` returns it as `("OK", [None])`. The
    earlier version re-raised on that one, aborting the pass on the standard
    case it was written to survive.
    """

    class _FakeClient:
        def __init__(self, typ, payload):
            self.typ, self.payload = typ, payload

        def uid(self, *_args):
            return self.typ, self.payload

    gone = [
        ("OK", [None]),                                    # RFC 3501 vanished uid
        ("NO", [b"[CLIENTBUG] FETCH Bad sequence"]),       # Yahoo's wording
        ("NO", [b"message not found"]),                    # NO with no code
    ]
    transient = [
        ("NO", [b"[UNAVAILABLE] Server busy"]),
        ("NO", [b"[OVERQUOTA] over quota"]),
        ("NO", [b"[INUSE] mailbox locked"]),
        ("BAD", [b"Command Argument Error"]),
    ]

    def _classify(typ, payload):
        mailbox = job_alerts.ImapAlertMailbox.__new__(job_alerts.ImapAlertMailbox)
        mailbox._client = _FakeClient(typ, payload)
        try:
            mailbox.fetch_raw(b"1")
        except job_alerts.MessageGone:
            return "gone"
        except Exception:
            return "transient"
        return "read"

    assert [_classify(*c) for c in gone] == ["gone"] * len(gone)
    assert [_classify(*c) for c in transient] == ["transient"] * len(transient)


def test_the_server_text_survives_the_raise() -> None:
    """The text is the only thing that distinguishes the two cases, so losing it
    is what made the earlier version unfixable."""
    mailbox = job_alerts.ImapAlertMailbox.__new__(job_alerts.ImapAlertMailbox)

    class _Busy:
        def uid(self, *_a):
            return "NO", [b"[UNAVAILABLE] Server busy, try again"]

    mailbox._client = _Busy()
    with pytest.raises(Exception) as caught:
        mailbox.fetch_raw(b"1")
    assert "UNAVAILABLE" in str(caught.value)
    assert not isinstance(caught.value, job_alerts.MessageGone)


def test_every_counter_in_the_report_reaches_the_totals() -> None:
    """`alerts_without_postings` is the alarm the module promises for a changed
    alert layout, and the scheduler logs `ingest_all`'s totals - not the
    per-account report. Leaving it out of the roll-up made the alarm invisible
    in the one place it matters."""
    campaign = seed_campaign("alerts-counters@example.test")
    seeker_id = campaign["seeker_id"]
    mailbox = FakeMailbox({
        "m1": _message("LinkedIn <jobalerts-noreply@linkedin.com>", LINKEDIN_HTML),
    })
    report = job_alerts.ingest_account(_account(seeker_id), backend=mailbox)

    import inspect
    source = inspect.getsource(job_alerts.ingest_all)
    aggregated = {k for k in report if k != "account_id" and f'"{k}"' in source}
    missing = {k for k in report if k != "account_id"} - aggregated
    assert not missing, f"these counters never reach the totals: {sorted(missing)}"


# --- the canonical URL is on a host the source really publishes on ---------------

def _glassdoor():
    return next(s for s in job_alerts.ALERT_SOURCES if s.key == "alert.glassdoor")


@pytest.mark.parametrize(
    ("label", "href"),
    [
        (
            "tracked redirector",
            "https://click.tracker.example/r?u=https://www.glassdoor.be"
            "/partner/jobListing.htm?jobListingId=1009123456&src=mail",
        ),
        ("direct", "https://www.glassdoor.be/partner/jobListing.htm?jobListingId=1009123456"),
    ],
)
def test_a_tracked_alert_link_resolves_to_the_posting_host(label: str, href: str) -> None:
    """The host comes from the source's own domain inside the href.

    Glassdoor alerts commonly route links through a redirector, and the link
    pattern matches the glassdoor host *inside* the URL.  Reading the host from
    the href's netloc instead synthesised the posting URL on the redirector's
    host - a dead link that looks canonical.
    """
    assert job_alerts._posting_url(_glassdoor(), href, "1009123456") == (
        "https://www.glassdoor.be/partner/jobListing.htm?jobListingId=1009123456"
    )


def test_an_href_with_no_posting_host_never_yields_a_third_party_url() -> None:
    """A spoofed or rewritten link falls back to the sender domain, not to its own host."""
    href = "https://evil.example/partner/jobListing.htm?jobListingId=1009123456"
    url = job_alerts._posting_url(_glassdoor(), href, "1009123456")
    assert "evil.example" not in url
    assert url == "https://glassdoor.com/partner/jobListing.htm?jobListingId=1009123456"
