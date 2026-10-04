# Dream Job

**AI-assisted job discovery and application platform.**

Most job search tools react to what has been published. Dream Job starts from
who you are and what you actually want, maps the relevant slice of the market —
*including companies that have not advertised* — judges each company's capacity
and likelihood to hire, and then removes the friction of applying.

It is built to the *Dream Job* Software Requirements Specification v0.3, and it
is opinionated about scope: it collects what your directives justify rather than
downloading the world.

---

## What it does

```
  PROFILE ──────→ PLAN ──────→ DISCOVER ──────→ APPLY ──────→ FOLLOW UP

  LinkedIn + CV   Structured    Company         Hiring         Responses
  merged, and     directives    profiles and    contacts,      recorded,
  the job you     bound the     five-year       validated,     patterns
  actually want   search        financials      then written   found
```

- **Builds a profile you can trust.** Your LinkedIn export and CV are merged
  (8 experience entries, 27 skills, a 1,163-character summary and a photo on the
  product owner's real pair), and every disagreement between them is surfaced
  for you to resolve rather than silently decided. The real pair produces 18
  conflicts — role dates, employer names, titles — and each is resolved with
  LinkedIn's value, the CV's value or your own.
- **Takes your dream job seriously.** A free-text statement, in your own words
  and language, becomes a structured model — target roles, role families,
  deal-breakers with a `detectable_from` list, values, implicit preferences —
  that drives matching, discovery and scoring.
- **Finds unadvertised openings.** For companies with no matching vacancy, it
  reasons from their finances, hiring signals, department map and competitors'
  hiring to propose roles they are likely to need. These are labelled
  everywhere, and the email never asserts a vacancy exists (FR-263).
- **Knows an agency from an employer.** Interim agencies and undisclosed
  employers are classified before their company is profiled, so a tailored CV
  is not addressed to a broker and a client's name is not invented.
- **Reads the accounts.** Five years of filings per company (BE/NL/GB/US, with
  OpenCorporates), turned into two scores — ability to pay and investment
  capacity — each justified with the actual figures. Missing filings are marked
  *estimated*, not guessed.
- **Writes the application.** A tailored CV, a company briefing and a motivation
  document for you, and an introduction email for them — four artefacts per
  opportunity. A deterministic factual-consistency check, with an LLM as one
  signal rather than the arbiter, blocks anything the profile does not support.
  A failed check can be overridden with a recorded reason; a leak or an objected
  contact cannot.
- **Learns what works.** Record what came back — including by phone, LinkedIn or
  an ATS portal — and it tells you which kinds of role and company actually
  reply, with sample sizes and Wilson intervals, and where to redirect. It
  refuses to advise below six resolved applications, and says so.
- **Helps you get the interview.** Reply classification and in-thread drafts, an
  interview brief, calendar slot extraction, mock interviews with feedback, and
  a negotiation brief built from ability-to-pay and your own directives.

**Nothing is sent without you approving it.** Scoring is advisory throughout, and
the mail transport defaults to dry-run (RK-05): no message can leave by accident.

---

## The five phases

| Phase | What happens | Screens |
|---|---|---|
| **1 · Profile** | Intake, conflict resolution, skills, evidence, personas, composite profile, dream job model | Profile, Composite, Dream job |
| **2 · Plan** | Structured directives, campaign plan, reuse report, collection from 29 sources | Directives, Campaigns, Apply browser |
| **3 · Discover** | Company domains, employer kind, profiles, five-year financials, signals, opportunities, ranking | Companies, Opportunities, Intelligence |
| **4 · Apply** | Contacts, validation, tailored CV, briefing, motivation, email, bulk approval, dispatch | Contacts, Applications, Mail |
| **5 · Follow up** | Responses, pipeline board, reply drafts, calendar, mock interviews, learning, monitoring | Pipeline, Responses, Insights, Monitoring |

The **Where I am** screen is the map: sixteen stages across the five phases,
each showing what is done, what is running and — when something is blocked —
*what would unblock it*. Every stage links to the screen that advances it, and a
one-line version of the map sits at the top of every working screen.

Press **`?`** on any screen for its instructions, or click the **?** beside any
label for that concept. 21 screens documented, 142 glossary terms, 305 inline
tips: the help is part of the product, not an afterthought.

---

## Architecture

React SPA · Python/FastAPI · SQLite (WAL) · DeepSeek (OpenAI-compatible) ·
Playwright over CDP.

```
frontend/src            React SPA, 24 lazy-loaded route screens
backend/dreamjob
  api/routers           22 routers, 337 routes
  pipeline              intake · enrichment · planning · collection · profiling
                        · domains · employer kind · financial · opportunities ·
                        scoring · contacts · generation · autopilot
  adapters              29 source adapters over one four-step contract
  documents             CV, briefing, motivation and email generation
  mail                  Gmail OAuth and Resend behind one interface
  postapp               pipeline board, replies, learning, mock interviews
  browser               CDP / Playwright attach-only automation
  intelligence          gap analysis, stepping stones, values, events
  monitoring            watchlist, digest, scheduler
  db/repositories       the only place SQL is written
  llm  egress  jobs  security  observability   the shared services
```

The design rests on funnelling each cross-cutting concern through exactly one
module, which is what makes the corresponding requirements enforceable rather
than aspirational:

| Every… | goes through | so that |
|---|---|---|
| SQL statement | `db/repositories` | isolation holds and PostgreSQL stays possible |
| outbound request | `egress/client.py` | robots.txt, rate limits and caching are not optional |
| LLM call | `llm/client.py` | budget, injection defence, redaction and audit always apply |
| external source | `adapters/base.py` | a new job board needs no pipeline change |
| long-running task | `jobs/runner.py` | everything is pausable and resumable |
| employer-kind verdict | `pipeline/employer_resolver.py` | no verdict without evidence |
| company-domain decision | `pipeline/company_domains.py` | a namesake domain is refused, visibly |

**Scale:** 219 Python modules / 110,773 lines · 140 JS modules / 40,650 lines ·
94 test files / 51,165 lines · **1,725 tests pass offline** · 87 tables ·
144 indexes · 34 migrations · 29 adapters · 24 versioned prompt templates.
**156 of 157 requirements (99%)** from the specification are cited in the
source: 103/104 Must, 44/44 Should, 9/9 Could.

### Sources

| Type | Count | Sources |
|---|---|---|
| ATS | 9 | Greenhouse, Lever, SmartRecruiters, Ashby, Recruitee, Personio, Workday, Teamtailor, Workable |
| Job boards | 9 | EURES, VDAB, Actiris, Jobat, StepStone, Indeed, Welcome to the Jungle, Arbeitnow, generic HTML |
| Registries | 5 | NBB, KBO/BCE, Companies House, KvK, SEC EDGAR |
| Directories | 1 | OpenCorporates |
| Website | 1 | bounded company-site crawler |
| News / events | 3 | RSS newsrooms, event radar, social event calendars |
| Compensation | 1 | Eurostat Structure of Earnings Survey |

Indeed, StepStone, SmartRecruiters and the social-event adapter ship disabled
until an administrator acknowledges their terms of service (IR-101).

**Contact discovery** does not stop at the company home page. It reads the
pages the home page links to, the site's **sitemap**, **schema.org JSON-LD**
and **`security.txt`**; it infers the domain's address convention from observed
addresses; and, for the large corporate sites that answer every fetch with an
F5/Cloudflare challenge, it can query a **search API** (Brave, Bing or Google
CSE — set it on the Contacts screen; off until a key is supplied, because the
engines disallow their HTML search endpoint). A blocked site is recorded as
`blocked`, not reported as having no address.

### Persistence

SQLite in WAL mode with a deliberate **one-writer** discipline; repositories are
the only place SQL is written, and return plain dicts. 87 logical tables across
34 forward-only migrations: 49 seeker-scoped, 37 shared knowledge-base, one
migration ledger and two FTS5 search tables, plus a `usable_contact` view that
encodes the objection rule at the database.

### Security

Argon2id passwords, optional TOTP MFA, client-bound HMAC sessions, AES-256-GCM
field encryption with per-seeker HKDF keys, append-only audit trail, and consent
records for the LLM transfer (CR-410) and LinkedIn automation (CR-401).
Credentials, secrets and the sealed profile sections are encrypted; the SQLite
file itself relies on disk encryption.

---

## Running it

**Requirements:** Python 3.11+, Node 18+, and a DeepSeek API key.

```bash
git clone <this repo> && cd dreamjob

cp .env.example .env
python3 -m dreamjob.security.crypto --generate-key   # paste both values into .env
$EDITOR .env                                          # add DEEPSEEK_API_KEY

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..

python3 scripts/bootstrap.py        # schema + first administrator account
./scripts/dev.sh                    # API :8000, SPA :5173 with /api proxied
```

| | |
|---|---|
| Application | **http://127.0.0.1:5173** |
| API | http://127.0.0.1:8000 |
| API reference | http://127.0.0.1:8000/docs |

Migrations and the source catalogue are applied automatically at startup, and
the monitoring scheduler autostarts in-process (`DREAMJOB_SCHEDULER_ENABLED`).
Create further accounts from the Admin screen.

### Optional, when you need them

**Browser automation** — for LinkedIn and Glassdoor, which block scrapers, and
for ATS form pre-fill. You launch your own browser and log in; the system
attaches and never sees your credentials.

```bash
./scripts/browser.sh          # dedicated profile, remote debugging port
```

> LinkedIn's user agreement prohibits automated access **including through a
> session you logged into yourself**. Your account may be restricted. Dream Job
> shows this warning before the first run and refuses to proceed until you
> acknowledge it. The decision is yours to make knowingly.

**Sending mail** — either backend:

- *Gmail OAuth* — set `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, then connect on
  the Mail setup screen. Replies land in your own inbox and are detected.
- *Resend* — set `RESEND_API_KEY`. Sends from your own domain; no inbox, so
  replies are recorded by webhook or entered by hand.

`DREAMJOB_MAIL_DRY_RUN` defaults to `true`: the whole path runs — recipient,
guard rails, MIME with the CV attached — and stops one step short of the wire.
Arming a real send is a deliberate change of that setting plus a restart.

**A local model** — for the privacy-sensitive steps (composite profile, CV,
motivation document), any OpenAI-compatible endpoint:

```bash
DREAMJOB_LOCAL_LLM_BASE_URL=http://127.0.0.1:11434/v1
DREAMJOB_LOCAL_LLM_MODEL=llama3.1
DREAMJOB_LOCAL_LLM_TASKS=profile,cv,motivation
```

**Semantic search** — optional embeddings, inert when unset:

```bash
DREAMJOB_EMBEDDINGS_MODEL=text-embedding-3-small
DREAMJOB_EMBEDDINGS_BASE_URL=...
python3 scripts/build_semantic_index.py
```

**Near-real-time vacancies** — on by default. The scheduler's `vacancy_refresh`
task runs every 10 minutes (`DREAMJOB_VACANCY_REFRESH_SECONDS`): it re-reads a
bounded, round-robin slice of the ATS boards behind existing ranked lists, and
each new posting is added to every following campaign under its directives,
scored, and announced once as a `new_vacancy` notification. Vacancy fetches
treat a cached board as fresh for one hour (`DREAMJOB_VACANCY_CACHE_TTL_SECONDS`);
after that it is revalidated, which costs a 0-byte 304 when nothing changed.

To discover beyond the boards already followed, turn on the continuous cycle in
**Admin → Continuous** with a 300 s interval. The UI receives updates live
through `/api/events/stream`.

The scheduler runs inside the API process and stops with it. On Windows, if it
must survive API restarts, set `DREAMJOB_SCHEDULER_ENABLED=0` and run
`python -m dreamjob.monitoring.scheduler --once` from Task Scheduler instead.

---

## Using it

A first pass, roughly in order:

1. **Profile** — upload your LinkedIn PDF export and your CV, resolve the
   conflicts, mark anything you never want disclosed.
2. **Dream job** — write it properly. It is the most influential thing you enter.
3. **Composite profile** — confirm or reject the online findings. Anything not
   matched to you with high confidence waits for your decision.
4. **Directives** — bound the search: titles, company type, location, work
   arrangement, compensation. Turn on discretion mode if you are employed.
5. **Campaigns** — review the plan, its cost and what will be reused, then launch.
6. **Opportunities** — the ranked list. Pin, re-order, discard. Your order wins.
7. **Applications** — read what was written before approving it; send one at a
   time or bulk-approve with the recipient summary.
8. **Responses** and **Insights** — record what comes back, including rejections.
   That is what the advice is built from.

Prefer fewer decisions? **Autopilot** runs profile → directives → plan →
collection → enrichment in one pass and stops at review, so you choose who to
write to rather than configuring each stage.

---

## Configuration

All runtime configuration is read once from `.env` / the environment into a
frozen settings object; nothing reads `os.environ` directly, so the Admin screen
can show what is in force. Groups include:

- **Core** — `DREAMJOB_ENV`, `DREAMJOB_HOST`, `DREAMJOB_PORT`, `DREAMJOB_DATA_DIR`, `DREAMJOB_DB_PATH`
- **Keys** — `DREAMJOB_MASTER_KEY`, `DREAMJOB_SESSION_SECRET`
- **LLM** — `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, cheap/strong model names, `DREAMJOB_LOCAL_LLM_*`, `DREAMJOB_EMBEDDINGS_*`, budget and cost
- **Mail** — `DREAMJOB_MAIL_BACKEND`, `DREAMJOB_MAIL_DRY_RUN`, `GMAIL_*`, `RESEND_API_KEY`, `DREAMJOB_SEND_DAILY_CAP`, send window and interval
- **Egress** — user agent, per-domain RPS, concurrency, cache and negative-cache TTL, `DREAMJOB_VACANCY_CACHE_TTL_SECONDS` (freshness window for vacancy fetches), robots and crawl-delay flags, raw-document retention
- **Browser** — `DREAMJOB_CDP_URL`, profile directory, human pacing bounds
- **Scheduler** — `DREAMJOB_SCHEDULER_ENABLED`, `DREAMJOB_SCHEDULER_TICK_SECONDS`, `DREAMJOB_VACANCY_REFRESH_SECONDS` (0 turns the vacancy refresh off)
- **Observability** — log directory, level, format, rotation, slow-request and slow-query thresholds

---

## Development

```bash
PYTHONPATH=backend python3 -m pytest tests/unit tests/integration -q -m 'not llm and not load'
python3 -m ruff check backend/dreamjob             # lint
cd frontend && npm run build                       # production build
python3 scripts/traceability.py                    # requirement coverage
```

Tests run against a migrated temporary database with no network access; LLM and
HTTP paths are stubbed, and network/load tests are deselected. A Playwright e2e
harness and persona generator live under `tests/e2e`.

**Adding a source** is one file: subclass `SourceAdapter`, implement
`plan / fetch / parse / normalise`, decorate with `@register_adapter`. It appears
in the catalogue at the next boot. A language is content only — the prompt
templates and `help/content.js`.

### Operator tooling

```bash
python3 scripts/bootstrap.py                 # schema + first administrator
python3 scripts/traceability.py              # requirement-to-file matrix
python3 scripts/build_semantic_index.py      # backfill the embedding table

# Widen the company inventory (run by hand):
python3 scripts/import_board_registry.py     # public ATS boards, monthly
python3 scripts/resolve_employer_seed.py --dry-run   # named large employers
python3 scripts/import_register_bulk.py      # bulk company register seed
python3 scripts/ingest_kbo.py                # KBO staging
python3 scripts/mine_compensation.py         # salary corpus from postings
python3 scripts/normalise_corpus.py          # normalise collected corpus
python3 scripts/backfill_scoring.py          # rescore after a model change
python3 scripts/e2e_persona.py               # end-to-end persona run
```

The board registry is built from public URL indexes and shaped like them:
Personio and Recruitee SMEs, Teamtailor's Nordic base, and the Greenhouse and
Ashby boards named in Hacker News threads. The large employers a Benelux search
is judged on — the consultancies, the integrators, the telcos — run Workday or
their own careers site and appear in none of those indexes. The seed script
closes that gap from `backend/dreamjob/pipeline/data/employer_seed.json`: it
reads each named employer's own careers page and confirms the board with one
request before storing it. No slug is ever guessed.

---

## Data protection

The product processes two kinds of personal data and treats them differently.

**Yours.** Encrypted at rest with a key derived for your account alone. Fields
you mark *do not disclose* are excluded from every generated document and
stripped before any prompt leaves the machine. Full export and complete erasure
are one call each — and erasure derives its table list from the live schema, so
a table added later cannot be silently missed.

**Hiring contacts'.** Only professional contact details are stored, kept to what
a professional introduction needs. Every introduction email offers a way to
object, and **an objection blocks that address permanently, for everyone** on the
installation, enforced in a database view rather than only in the interface.
Contacts gathered through browser automation expire with the campaign that
collected them (30 days by default) and are never shared between job seekers.

DeepSeek processes data outside the EU. You are told before any profile data is
sent, and your consent is recorded (CR-410). Privacy-sensitive steps can be
routed to a local model instead. See [DPIA.md](docs/DPIA.md).

---

## Known limitations

Stated rather than glossed:

- **Passkeys (NFR-202)** are not implemented; the password + MFA branch is. A
  `webauthn_credentials` column exists but no code path uses it.
- **The SQLite file is not encrypted.** Credentials, secrets and sealed profile
  sections are field-encrypted; whole-database confidentiality rests on disk
  encryption. SQLCipher would add a dependency.
- **France and Germany** have no financial-registry adapter; companies there take
  the estimated path.
- **Employer-kind enforcement is partly wired** — scoring excludes an
  intermediary's company dimensions, but company profiling and financial passes
  do not yet skip an agency outright.
- **Semantic search is built but not consumed** by scoring or the API yet.
- **Microsoft 365 mail is not implemented**, and the Microsoft 365 calendar
  Graph client cannot be connected because its OAuth exchange is not exposed.
- **DuckDuckGo search** returns a bot-check page from datacentre ranges, so
  profile enrichment degrades to declared and handle-derived URLs there.

---

## Documentation

| | |
|---|---|
| [Functional Design](docs/Functional_Design.md) | What the product does, screen by screen, and every judgement call |
| [Technical Architecture](docs/Technical_Architecture.md) | How it is built, and why it is built that way |
| [DPIA](docs/DPIA.md) | Data-protection impact assessment (NFR-304) |
| [Collection outcome states](docs/Collection_Outcome_States.md) | What every collection result means |
| [Employer kind results](docs/Employer_Kind_Results.md) | Agency classification, measured |
| Word editions | `docs/Dream_Job_Functional_Design.docx`, `docs/Dream_Job_Technical_Architecture.docx` — illustrated, generated from the markdown by `docs/build_fdd.py` and `docs/build_ta.py` |

---

## Licence and status

Built to the *Dream Job* Software Requirements Specification v0.3.
Not yet production-deployed; the DPIA must be completed and reviewed first
(NFR-304), and the one requirement not cited in code is the DPIA itself.
