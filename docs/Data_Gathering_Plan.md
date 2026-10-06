# Data Gathering Plan — reaching 10,000 jobs from 7,500 companies

*Decision document, 2026-09-09. Synthesises three independent evaluations (sources, funnel, caching) of the first data-gathering phase. All endpoint figures were measured from this machine on 2026-09-09 with the product's own User-Agent and no credentials; probe scripts and raw results are in the session scratchpad (paths in the appendix). No application code was changed.*

---

## 1. The answer

**Both, but not in equal measure — and neither is the reason the number is zero.** The corpus is empty today because no stage of the pipeline turns a company into a fetchable target: the planner emits one plan item per adapter, all seven ATS items and every registry/crawler item receive an empty target list, the keyword-shaped boards all point at dead or blocked endpoints, and an ATS item that did carry slugs would read only the first one — so the first and largest change is a **discovery stage** that feeds a pre-built registry of ~14,700 live ATS boards and the live EURES API into the planner, which is new plumbing around existing sources more than it is new sources (two small keyless adapters, Workable and Teamtailor, and one for Actiris, are the only genuinely new feeds worth writing). **Looser filtering is second-order**: the caps must be raised (`max_pages` 200 → 10,000, `max_companies` 200 → 8,000), and three silent losses must be fixed (single-slug ATS items, per-adapter knowledge-base reuse that skips a whole vendor on the second run, a dedup threshold that discards 17% of real openings) — but none of these moves the count off zero on its own, and the caps are off by ~40× on companies, not the two orders of magnitude the brief assumed. With discovery in place, **7,500 companies and far more than 10,000 jobs are reachable in one four-hour campaign at the product's existing, compliant rate limits** (EURES employers are the long pole at europa.eu's `Crawl-delay: 10`; ATS vendors run in parallel at 0.5 req/s each), and the job count stops being a target at all: 7,500 companies necessarily bring 50,000–100,000 openings, because an ATS board averages ~15 jobs and the 1.3-jobs-per-company ratio in the target is an aggregator shape that only EURES has.

What that means in practice:

- **Do first (cheap):** re-point EURES at its live API, raise the caps, raise the dedup threshold, delete two leaked test rows from the catalogue. Half a day. This alone takes the pipeline from 0 to ~10,000 jobs / ~2,500 employers in 33 minutes.
- **Do second (the real work):** a board registry + deterministic per-target planning + company-row creation + domain-parallel collection. Two to three weeks. This reaches 7,500 companies.
- **Do third (keeps it cheap on day 2):** per-target knowledge-base reuse with a fetch ledger and conditional GETs. One week. Without it, every campaign after the first either re-downloads everything or wrongly skips it.

---

## 2. The arithmetic

### 2.1 The shape problem

"10,000 jobs from 7,500 companies" is 1.3 jobs per company. No source with company identity produces that ratio:

| Source type | Jobs per company (measured) | 7,500 companies would bring |
|---|---|---|
| ATS boards, random registry sample (n≈250 boards, 8 vendors) | mean ~15, median ~7 (Greenhouse 18/7, Ashby 13.3/8, Lever 14.6/8, Workable 12/3.5, Recruitee 10.2/4, Personio 7.8/5, Workday 98/33) | ~110,000 |
| ATS boards, curated Benelux/EU tech (n=50, guessed slugs of known names) | mean 77, median 30–54 — selection bias toward large, known companies | ~400,000 |
| EURES (public-employment-service aggregator) | ~5 rows per distinct employer on shallow pages (20–26% distinct), collapsing on deep pages | ~37,500 rows minimum |

The two evaluations that measured jobs-per-board disagree (15 vs 77) only because they sampled different populations; both are right for what they sampled. **This plan uses 15 for registry boards and 40 for hand-curated Benelux boards.** Either way: the company count is the design variable and the request driver; the job count is a by-product that overshoots by 5–10×. The overshoot costs disk (~200 KB per board) and dedup CPU, not requests, and a filtered corpus is what the enrichment phase wants anyway.

### 2.2 The rate constants everything is priced in

| Constraint | Value | Requests per 4 h per bucket | Source |
|---|---|---|---|
| `DREAMJOB_PER_DOMAIN_RPS` (DomainLimiter, keyed by netloc) | 0.5 → 2.0 s between requests | 7,200 | `egress/client.py:90`, `.env` |
| europa.eu `robots.txt` `Crawl-delay: 10` under `User-agent: *` | 10 s | 1,440 | curl 2026-09-09; **not honoured by DomainLimiter today** |
| api.lever.co `Crawl-delay: 1` | 1 s (0.5 rps is already compliant) | — | robots.txt |
| Per-tenant vendors (`*.recruitee.com`, `*.jobs.personio.de`, `*.myworkdayjobs.com`, `*.teamtailor.com`) | each tenant is its own netloc, so the limiter never waits; Recruitee answered 429 on 11/56 at concurrency 10 | budget as one 0.5 rps bucket per vendor | `probe_tenant_hosts.py` |
| Collection loop | strictly serial, grouped by adapter; `http_max_concurrency=20` is never used | wall-clock = **sum** of buckets | `pipeline/collection.py` collection_worker |

Note the inconsistency: the catalogue records `rate_limit_rps = 1.0` for the seven ATS hosts and `estimate()` prices plans at that rate, but the limiter uses the global 0.5. All arithmetic below is at **0.5 rps** (the conservative value that actually runs) and at **10 s for europa.eu** (which the product should honour — see §6). Moving ATS hosts to the catalogue's 1.0 rps would halve every ATS bucket time; it is defensible for JSON APIs whose robots.txt states no delay, but it is a follow-up, not an assumption.

### 2.3 What exists, legally reachable, without credentials

| Pool | Companies | Jobs | Evidence |
|---|---|---|---|
| ATS board registry (Common Crawl index + Wayback CDX + HN "Who is hiring"), 9 vendors | 15,922 slugs enumerated in ~65 requests; **~14,600 live** (CC-2026 slugs 87.5% live, Wayback-only 27.5%) | **~220,000–310,000** (Workday's mean of 98 is skewed; its median gives the low end) | `cc_slugs.py`, `wayback.py`, `hn.py`, `verify_boards_result.json` |
| — of which EU-relevant | ~3,000–5,000 boards (Recruitee 83% EU, Personio 55%, Ashby 18%, Lever 15%, Greenhouse 8%; Workable/Teamtailor unmeasured, likely high) | ~65,000 EU jobs | job-location shares per vendor |
| — of which Benelux | **~900–1,500 boards** | ~10,000 Benelux jobs | Recruitee 37% Benelux, Personio 6%, Ashby 4.3%, Greenhouse 1.6%, Lever 0% |
| EURES live API (`POST /eures/api/jv-searchengine/public/jv-search/search`) | BE 232,496 vacancies (69,687 last week); NL 270,984; EU 2,041,695; employer name + description inline, 50/page, 200 pages/query max | all of the above | `probe_eures_*.py`, `eures_density.py` |
| Actiris (Brussels PES) sitemap + server-rendered detail pages | 22,950 offers (18,073 modified since July); 25/25 sampled offers had distinct employers | ~23,000 | `actiris_sample.py` |
| Arbeitnow API | 1,632 companies / 3,300 jobs in 30 requests (German-centric) | | `arbeitnow2.py` |
| SEC EDGAR / Companies House / KBO bulk files | millions of identities, 0 jobs, 0 careers URLs | | §6 |
| VDAB, Jobat, StepStone, Indeed, WTTJ, SmartRecruiters API, Le Forem, werk.nl, Remotive | **0** under FR-182 / IR-101 (robots, ToS, login walls, Cloudflare 403) | 0 | `probe_robots.py`, `logs/app.log` |

### 2.4 Campaign A — the target, in one four-hour window

Composition chosen for a BE/NL-directed campaign: all of the EU-heavy vendors, a first tranche of the three large US-heavy vendors, a partitioned EURES sweep, and a first Actiris tranche. Each row is one rate-limit bucket; buckets run in parallel (§5 N5), so wall-clock is the longest row, not the sum.

| Bucket | Requests | Interval | Bucket time | Companies | Jobs (raw) |
|---|---|---|---|---|---|
| europa.eu — EURES BE+NL, 133 partitions (NUTS-1 × NACE Rev 2.1 section, excluding only T/U/V) × ≤ 4 pages, `LAST_MONTH` | 600 | 10 s | **100 min** | 3,000–5,000 | 30,000 |
| `*.jobs.personio.de` (all live) | 1,325 | 2 s | 44 min | 1,325 | 10,300 |
| `*.recruitee.com` (all live) | 850 | 2 s | 28 min | 850 | 8,700 |
| `*.teamtailor.com` (all, new adapter) | 490 | 2 s | 16 min | 370 | 3,800 |
| api.lever.co (all live) | 277 | 2 s | 9 min | 277 | 4,000 |
| api.ashbyhq.com — first 1,000 of 4,000 live | 1,000 | 2 s | 33 min | 875 | 11,600 |
| boards-api.greenhouse.io — first 1,000 of 3,400 live (`content=true`) | 1,000 | 2 s | 33 min | 875 | 15,800 |
| apply.workable.com — first 1,000 of ~2,500 live (new adapter) | 1,000 | 2 s | 33 min | 900 | 12,000 |
| www.actiris.brussels — sitemap + 1,000 newest offers (new adapter) | 1,001 | 2 s | 33 min | 800 | 1,000 |
| **Sum** | **7,543** | | **100 min wall-clock (parallel)** | **9,300–11,300 before cross-source dedup → ~8,500–10,500** | **~97,000 raw → ~93,000 after dedup** |

- Companies: **≥ 7,500 with 1,000–3,000 of margin**, even at the low end of the EURES range. Cross-source overlap (an employer on EURES that also has a Recruitee board) is estimated at 5–10%; it is small because the ATS pool is mostly non-Benelux.
- Jobs: ~93,000, of which ~35,000 are located in Benelux (EURES 30,000 + Actiris 1,000 + ~4,600 from ATS boards). The 10,000 target is passed nine times over.
- Wall-clock under today's serial, adapter-grouped loop: 600×10 + 6,943×2 = 19,886 s = **5.5 h — over NFR-103**. With domain interleaving (sort plan items round-robin by netloc, a small change) the loop approaches the parallel figure; with one task per bucket it is exactly 100 min.
- LLM tokens: **0** for collection (ATS, EURES, Actiris parse deterministically, `llm_fallback = False`); planning ~10–20k tokens (≈ EUR 0.01) because registry-fed items are generated without the LLM.
- Writes: ~97,000 vacancy rows at 0.3–4 ms each = 1–7 min; the single SQLite writer (CR-408) is not a constraint. One index is required first (§5 N7): without `vacancy(company_name_raw, collected_at)` the unresolved-company candidate scan costs 18 ms per new vacancy at 100k rows ≈ 30 min.
- Disk: ~6,900 boards × ~200 KB + EURES 600 × ~100 KB + Actiris 1,000 × ~50 KB ≈ **1.5 GB** of `raw_document` per full fetch. Greenhouse with `content=true` is the heavy one (268 KB mean, 3.6 MB for GitLab, 5.7 MB for Cloudflare).

### 2.5 Campaign B — 10,000 jobs as fast as possible

EURES BE, `LAST_WEEK`, partitioned by NUTS-1 × NACE: **200 requests × 10 s = 33 min → 10,000 rows, ~2,000–2,600 distinct employers, 0 tokens.** (7 min if europa.eu's Crawl-delay were ignored; it should not be.) This is the acceptance test for the cheap changes in §5 C1–C5 and needs no new code beyond the partitioned query shape.

### 2.6 Campaign C — 7,500 *Benelux* companies

If the product owner means 7,500 companies with a Benelux footprint, ATS boards cannot carry it (900–1,500 exist across all vendors) and the sum is:

| Source | Requests | Time | Benelux companies |
|---|---|---|---|
| EURES BE+NL, full partition sweep | 1,400 | 3.9 h at 10 s | 5,000–8,000 |
| Actiris, 2,000 newest offers | 2,001 | 67 min | ~1,500 |
| ATS boards with ≥ 1 BE/NL/LU location (Recruitee, Personio, Teamtailor, Ashby whole; filter after fetch) | ~6,700 | 2.2 h (Ashby bucket) | 900–1,500 |
| **Sum** | | **3.9 h wall-clock** | **7,400–11,000** |

Reachable in one window only at the top of the EURES range; **comfortable over two nightly runs**. The one number that settles it: run the partitioned EURES sweep once and count distinct normalised employer names. Two measurements exist (26% distinct on 200 rows; 20.5% on 1,000 rows, falling to ~1% on deep unpartitioned pages) and neither measured a partitioned sweep. Everything above is priced at 20% on the first 10–20 pages of each partition.

### 2.7 Day two and after

Vacancy staleness is 7 days, so each board is re-checked weekly. With the fetch ledger and conditional GETs (§4, §5 N4): unchanged boards cost one 0-byte 304 each — still 2 s of bucket time, so a weekly revalidation of 6,900 boards is ~45 min wall-clock in parallel (Personio bucket), ~4 h serial — and EURES `LAST_WEEK` partitions are ~200 requests = 33 min. Run it as a scheduled refresh outside the campaign clock; campaigns then read the knowledge base (FR-342) and issue ~0 requests for targets inside the window. **Without** N4, a second campaign within 24 h serves everything from `http_cache` but drops provenance; after 24 h it re-downloads ~1.5 GB; and the knowledge-base reuse logic wrongly skips or wrongly re-collects (§3 step 6).

### 2.8 Not reachable in four hours, and when it arrives

| Goal | Why not in 4 h | Horizon |
|---|---|---|
| 7,500 Benelux companies each with a readable ATS board | only ~900–1,500 exist | never; Benelux identities come from EURES/Actiris/KBO |
| KBO number / VAT / domain for 7,500 EURES employers | `registry.kbo` at 0.5 rps = 4.2 h alone; NBB/KvK/OpenCorporates need keys | KBO monthly open-data CSV (free account) as a bulk import, matched by name — phase 2, days not hours |
| Company names for Greenhouse/Ashby/Lever/Workday boards | payloads carry no organisation name; Greenhouse needs `/v1/boards/{slug}` (1.8 h for 3,217 boards) | once per 90 days in the registry refresh, not per campaign; Ashby/Lever/Workday take the name from the registry seed or the HN thread |
| Bulk board discovery through `detect_ats` on company websites | 6.7% hit rate, ~30 requests per board found; 7,500 companies = 15,000 requests = 8.8 h for ~500 boards | never for bulk; keep for named target companies |
| Full registry (14,600 boards) in one campaign | 2.2 h parallel (Ashby bucket), 6.3–8.4 h serial | nightly refresh job, 2.2 h |

---

## 3. The funnel today

Belgium campaign `be08041d…`: 21 plan items, 0 vacancies written. All 36 vacancy rows in `data/dreamjob.db` are `source_adapter='e2e-fixture'`; no adapter has ever written one. Multipliers are on productive yield, not row counts.

| # | Step | What happens today | Multiplier today | After the changes in §5 |
|---|---|---|---|---|
| 0 | Available, legal, keyless | EURES BE 232k + NL 271k; ~14,600 live ATS boards, ~300k jobs; Actiris 23k | ceiling | — |
| 1 | Catalogue selection (FR-164) | 26 rows → 21 selected; rejects Indeed (prohibited), StepStone/SmartRecruiters/Social (ack), Companies House/EDGAR (GB/US). Two leaked test rows (`broken_board`, `stub_board`) planned as real sources | ×1.0 on productive yield | ×1.0 (delete the stubs) |
| 2 | Plan generation (FR-162) | one item **per adapter**; targets from directives only; prompt rule 3 forbids inventing companies → 14/21 items have empty `board_slugs` / `legal_ids` / `crawl_seeds` / `companies`; the other 7 hit dead endpoints | **×0** | one item per (adapter, target), deterministic, 0 tokens |
| 3 | ATS slug handling | `slug_of()` returns `board_slugs[0]` → ≤ 6 boards per campaign even with a populated plan (xfail test already documents it) | ≤ 6 boards ≈ 250 jobs | all slugs, one item each |
| 4 | Endpoint liveness | EURES `DEFAULT_API_BASE` → 404; VDAB listing is a JS shell and its API is robots-disallowed; Jobat 403 ×60; WTTJ 403 without keys; SmartRecruiters API robots `Disallow: /` | **×0** | EURES live; the rest stay 0 by design |
| 5 | Caps (FR-186) | ATS "page" = whole board (pagination=False) so `max_pages 200` = 200 boards ≈ 3,000 jobs at registry mean; `max_companies 200` compares against company rows that vacancy writes never create; `max_pages_per_source 20` is per plan item | ×0.3 jobs, ×0.03 companies (had 2–4 worked) | ×1 with §7 |
| 6 | Knowledge-base reuse (FR-342) | `assess_reuse` counts fresh rows **per adapter** against `pages × min(100, per_page)`; after any run writes ≥ 100 fresh `ats.greenhouse` rows every Greenhouse item is `skipped` for 7 days; a never-seen board with `expected=1` is also skipped; a seen board is re-collected with zero page reduction | **×0 on run 2** | key on target |
| 7 | Fetch | serial, 2.0–3.4 s per board measured (2 s floor + body); 200 boards = 11 min; 7,500 = 4.2–7.1 h; `http_max_concurrency=20` unused | ×1 for ≤ 2,000 pages; fails NFR-103 above | parallel buckets: wall-clock = longest bucket |
| 8 | Parse / normalise | deterministic, 0 LLM tokens, 0 parse errors in logs; but a 403/404 board yields 0 rows and is reported `done` with `error_count 0` | ×1 on live boards, ×0 silent on dead ones | dead boards flagged `failed` |
| 9 | Dedup (FR-184) | threshold 0.86 with a 0.6 "unknown location" bonus: 170/942 real board rows (18%) merged wrongly ("Data Engineer" Brussels ↔ Ghent; "Engineer 6" ↔ "Engineer 7" at 0.955) | **×0.82** | ×0.955 at 0.92 + location veto + numeric-token guard |
| 10 | Write | 0.3–4 ms/row; no company row is created from `company_name_raw` → `stats.companies` stays 0 | ×1 jobs, **×0 companies** | stub company per employer / per (vendor, slug) |
| | **Net** | | **232k+ available → 0 written** | Campaign A: ~93,000 jobs, ~8,500–10,500 companies |

The funnel is not narrowed by filters. Steps 2–4 are absences and dead ends, not filters; step 5 is the only place where "looser" is the right word, and it only matters once steps 2–4 exist.

---

## 4. Caching

| Layer | Verdict | What works | What re-fetching still happens |
|---|---|---|---|
| **L1 `http_cache`** (24 h TTL, exact URL) | partial | hits served from DB across client instances; 24 h TTL enforced; non-200 excluded; robots cached once | after 24 h every URL is re-downloaded in full: `etag` / `last_modified` are stored but **never sent**, though all three big ATS vendors answer a conditional GET with a 0-byte 304 (GitLab 3.59 MB → 0 B). Cache key is not normalised (`?x=1&y=2` vs `?y=2&x=1`, trailing slash, host case all miss) and a redirect target is not keyed. **Every hit returns `raw_document_id=None` and `headers={}`**, so 100% of records produced from cached pages lose FR-183 provenance (25 call sites propagate it). |
| **L2 `raw_document`** (content-hash store) | works for dedup; leaks | identical bodies stored once; 10,000 unchanged refetches → 0 new files | every non-200 body is stored (33 of 52 live rows; one jobat URL has 8 Cloudflare challenge pages differing only in Ray ID); nothing is ever evicted; a weekly refresh of changed boards orphans ~160 MB/week (~8 GB/year) referenced by nothing |
| **L3 knowledge-base reuse** (FR-342, staleness 7/90 days) | **does not work for vacancies**; works for company profiles | `build_profile` correctly reuses a company with a summary refreshed < 90 days | reuse is `min(expected, fresh_rows_for_this_adapter)`: re-planning the same board → `collect_partial`, zero page reduction, full re-fetch, provenance doubled (227 → 454); planning a never-seen board with `expected=1` → **skipped**; the headline said "nothing reusable yet" on a run served 100% from cache |
| **L4 within one run** | works only by accident | serial fetching means the second identical URL is an L1 hit | `fetch_many([url]×5)` → 5 server hits; no single-flight; adding the concurrency §2 needs would re-introduce N-1 duplicate fetches for shared URLs (e.g. several adapters probing one careers page) |
| **Scale** | fine | 100k `http_cache` + 100k `raw_document`: hit 144 µs, miss 12 µs, indexed; writer 0.36 ms/page | one un-indexed path: `vacancy_candidates` without `company_id` scans `company_name_raw` (18 ms/call at 100k rows) |

**What caching can and cannot buy.** It cannot bring a day-2 refresh of 7,500 boards under NFR-103 on its own: the limiter charges 2 s per request per domain whether the answer is 3.6 MB or a 0-byte 304 (7,499 × 2 s = 4 h 10 min on one host). Only *not issuing the request* — per-target reuse keyed on (adapter, board slug / employer / query URL) inside the staleness window — gets a repeat campaign to minutes. Conditional GETs then cover the weekly revalidation at ~0 bytes for unchanged boards. Ranked by re-fetching prevented: (1) per-target reuse via a fetch ledger, (2) conditional GET + 304 handling, (3) `raw_document_id` + headers on cache hits, (4) stop storing non-200 bodies and negative-cache 404 probes for 7 days (prevents up to 4 × 7,500 = 30,000 repeated RSS feed probes per enrichment pass), (5) 30-day orphan pruning, (6) single-flight coalescing + key normalisation, (7) persisted egress stats and provenance upsert, (8) two indexes.

---

## 5. What to change

### 5.1 Config changes — hours, do first

| # | Where | Change | Why | Unlocks | Effort / risk |
|---|---|---|---|---|---|
| C1 | `adapters/jobboards/eures.py:62` `DEFAULT_API_BASE` | `https://europa.eu/eures/eures-apps/searchengine/page` → `https://europa.eu/eures/api/jv-searchengine/public` | old path 404s (GET and POST); new path answers the adapter's own `search_body` shape with `jvs[50]` | Campaign B: 10,000 jobs in 33 min | trivial; add a recorded-response fixture test so shape drift is caught |
| C2 | `eures.py:106` `fetch_details` | `True` → `False` | description (median 1,533 chars, 0 empty), `employer.name`, `locationMap`, dates are inline; `jv-details` on the new base is 404 | ×51 fewer requests per 50 jobs | none |
| C3 | `pipeline/planning.py:50` `DEFAULT_CAPS` | see §7 | 200 boards / 200 companies cannot express the target | the target | none by itself; the plan UI must aggregate thousands of one-page items by adapter |
| C4 | `pipeline/dedup.py:263` `VACANCY_MATCH_THRESHOLD` | `0.86` → `0.92` | 18% false merges on real boards; 0.92 retains 95.5% | +1,300–1,600 kept per 10,000 fetched | reposts are still caught by `dedup_key`; re-run the dedup fixtures |
| C5 | `source_catalogue` rows `broken_board`, `stub_board` | delete; point tests at a scratch DB | test fixtures leaked into production and were planned as real sources | 2 wasted plan items | none |
| C6 | `source_catalogue` `ats.smartrecruiters` | `enabled=0`, `legal_notes`: "api.smartrecruiters.com robots.txt: Disallow / for all but LinkedInBot" | it advertises itself as a keyless API and silently collects nothing | honesty in the FR-185 dashboard | none |
| C7 | `planning.py:65–67` and `estimate()` | 0 LLM tokens and 1 s overhead for deterministic adapters (ATS, EURES, Actiris, Arbeitnow) | at 8 s and 2,900 tokens per page a 7,500-board plan displays **16.7 h and 21.75 M tokens** (7,500 × 2,900; the 18.75 M once quoted here counted only `TOKENS_PER_PAGE_IN`) and trips the budget check | plans that are not rejected | 10 lines |
| C8 | `pipeline/knowledge_base.py:39` | add `"board_registry": 30` | registry liveness decays (87.5% → 27.5% for stale slugs) | monthly re-verification | none |
| C9 | `.env` | keep `DREAMJOB_PER_DOMAIN_RPS=0.5`, `DREAMJOB_HTTP_MAX_CONCURRENCY=20`, `DREAMJOB_HTTP_CACHE_TTL_SECONDS=86400`; add `DREAMJOB_NEGATIVE_CACHE_TTL_SECONDS=604800`, `DREAMJOB_RAW_DOCUMENT_RETENTION_DAYS=30` | the new keys need N7 to take effect | — | none |

### 5.2 New code — ranked by yield per unit of effort

| # | What | Why | Unlocks | Effort | Risk |
|---|---|---|---|---|---|
| **N1** | **Discovery stage + per-target planning + company rows.** Before FR-162 translation, materialise targets: registry boards → one ATS plan item per (vendor, slug) with `estimated_pages=1`, generated deterministically (no LLM); EURES → one item per (NUTS-1 × NACE section × period); existing company rows with `ats_slug`; the user's named companies via `detect_ats`. Fix `slug_of()` or emit one item per slug. In `_write_vacancy`, create a company row when `resolve_company` misses — keyed on `(ats_vendor, ats_slug)` for boards, on normalised `company_name_raw` + country for EURES/Actiris — and add `(ats_vendor, ats_slug)` as a DR-101 dedup key. | the actual cause of zero (§3 steps 2, 3, 10); `adapter.plan()` / `company_targets()` already exist and are unit-tested but nothing in the pipeline calls them | every ATS, registry and crawl source; makes `max_companies` measure something | **medium** (1–2 weeks) | plan volume: thousands of items per campaign — paginate the plan view and checkpoint per item (the checkpoint/resume model already fits one-page items) |
| **N2** | **EURES partitioning + Crawl-delay.** Plan items per NUTS-1 (`be1/be2/be3`, NL equivalents) × NACE section × `publicationPeriod`, ≤ 20 pages each. **Correction (2026-09-09):** the sector letters are NACE **Rev 2.1**, not Rev 2 — N is professional, scientific and technical activities (69–75) and **O** is administrative and support services (77–82), which is where staffing sits. The "86,014 of 232,496 BE rows" measured N, i.e. professional services; skipping N removed *no* agency row and silently discarded engineering consultancies, law firms and architects. Neither letter is excluded now: a vacancy is filed under every section on its employer's NACE list, so excluding O would lose whole legitimate sectors and still not remove agencies (NOEL FRANKLIN's 56 rows arrive under C, F, N and O). Agencies are handled by the per-employer tag `company_employer_kind` (docs/Interim_Agencies_Proposal.md §2.5), and the lever for a broad, low-yield partition is its **page budget** — O carries 86% of Flanders' rows and gets half a partition's pages (`discovery.SECTION_PAGE_WEIGHT`), never an exclusion. Add `Crawl-delay` parsing to `DomainLimiter` (europa.eu 10 s, api.lever.co 1 s). | employer density collapses after ~20 pages of one list (205/1,000 shallow → 4/300 deep); keyword queries return 400/500 on the new API, sector/region/period filters work | 3,000–8,000 Benelux employers; keeps the product compliant with europa.eu's stated wish | small–medium | Crawl-delay makes EURES the long pole (100 min for 600 requests); acceptable |
| **N3** | **Board registry importer** (`scripts/import_board_registry.py`, operator-run) → `pipeline/data/board_registry.json` (vendor, slug, name, first_seen, last_verified, source). Reads the Common Crawl index (~60 requests/crawl), Wayback CDX with `showNumPages/page` pagination (3 requests, 64–195 s each — over the 30 s egress timeout), HN Who-is-hiring via hn.algolia.com (14 requests/year). Monthly refresh; liveness re-verified with one request per slug in the nightly job. | 15,922 slugs / ~14,600 live in ~65 requests; the only route to thousands of boards | 7,500 companies | medium (1 week) | **must not run through the egress layer**: index.commoncrawl.org and data.commoncrawl.org publish `User-agent: * Disallow: /`. The importer is a legitimate use of an open dataset under Common Crawl's terms of use, but keeping it outside campaigns keeps the FR-182 invariant ("the app never fetches a robots-disallowed URL") absolute and auditable. Cite the ToU in the script header. |
| **N4** | **Fetch ledger + per-target reuse + conditional GET.** Table `(adapter_key, target_key, url, etag, last_modified, fetched_at, http_status, record_count)` written by the collection worker; `assess_reuse` and `_pages_for` skip a target whose `fetched_at` is inside the staleness window; on TTL expiry send `If-None-Match` / `If-Modified-Since`, on 304 keep the body and extend `expires_at`; store `content_hash` and `raw_document_id` in `http_cache` and return them on a hit; provenance becomes an upsert on `(entity_type, entity_id, source_plan_item_id, adapter_key)`; persist egress stats per plan item. | §4 L1 and L3 | second campaign: ~0 requests for fresh targets, ~0 bytes for unchanged boards, provenance intact, truthful reuse headline | medium (1 week) | none; all additive |
| **N5** | **Domain-parallel collection.** Either sort plan items round-robin by netloc (cheap) or run one task per rate-limit bucket under the existing 20-slot semaphore (robust). Add vendor-group buckets (`*.recruitee.com`, `*.jobs.personio.de`, `*.myworkdayjobs.com`, `*.teamtailor.com` → one 0.5 rps bucket each). Add single-flight coalescing in `EgressClient.fetch` (in-flight `{url_hash: Future}`) and normalise the cache key **before** any fan-out. | Campaign A is 5.5 h serial, 100 min parallel; Recruitee 429s at 10 concurrent tenants | NFR-103 for Campaign A; the full registry in 2.2 h | medium (3–5 days) | the KB writer is already a serialised single writer, so concurrent collection is safe; without the group buckets, per-tenant vendors will rate-limit the product's IP |
| **N6** | **Dedup guards.** Location veto (both locations known and different ⇒ never merge); treat any differing numeric token as a level/reference marker (today only 1–5 and i–v are protected); drop or shrink the 0.6 unknown-location bonus. | "Data Engineer" Brussels ↔ Ghent merged at 0.873; "Engineer 6" ↔ "Engineer 7" at 0.955; 227 synthetic titles collapsed to 6 rows | retention 82% → ≥ 95% | small (1–2 days) | some genuine reposts survive as separate rows; `dedup_key` still catches exact ones |
| **N7** | **Hygiene.** Count a non-2xx or robots-blocked board as a plan-item error and mark `failed` when a company-lookup item returns 0 rows from a non-2xx; stop persisting non-200 bodies (or first-per-URL+status only); negative-cache 404/410 for 7 days and 429/5xx for 6 h; 30-day orphan pruning of `raw_document`; indexes `vacancy(company_name_raw, collected_at)` and `provenance(entity_type, created_at, adapter_key)`. | §3 step 8, §4 L2, the 18 ms scan | truthful coverage; ~8 GB/year not orphaned; 30 min of scan time per 100k rows | small (2–3 days) | none |
| **N8** | **Four keyless adapters.** `ats.workable` (`POST apply.workable.com/api/v3/accounts/{slug}/jobs`, 10/page with `total`; robots `Disallow:` empty; 3,047 slugs, 30/30 answered); `ats.teamtailor` (`{slug}.teamtailor.com/jobs.rss`; 487 slugs, 20/20 answered); `board.actiris` (sitemap `sitemapoffers-nl/fr.xml`, filter `lastmod ≥ 60 days`, detail pages ordered by lastmod, 50 offers per plan "page"; robots disallows only `/media/`); `board.arbeitnow` (250/page; terms: free public API, do not abuse, link back). | 3,500 boards / ~40,000 jobs; ~23,000 Brussels offers with SME employers; ~1,600 companies per 30 requests | Campaign A rows 4, 8, 9 | medium (1 week for all four) | Actiris is 1 request per offer at 2 s: 7,200 offers per 4 h is its ceiling |
| N9 | `detect_ats` fixes: add `careers-analytics`, `cdn`, `static`, `assets`, `app` to `_NOT_A_SLUG`; verify a detected slug with one API request before storing. Keep it for named targets and the watchlist only. | it returned slug `careers-analytics` for two Belgian companies (Recruitee's analytics host → 403) | correctness for user-named companies | small | none |
| N10 | `RegistryAdapter.import_bulk(path)` for KBO (monthly CSV, free account) and Companies House (492 MB zip, no key): company rows keyed by `legal_id`, used for DR-101 identity and NACE filtering of board-derived companies. | registers are bulk files, not per-company requests | identities for the enrichment phase | medium | phase 2; 0 jobs |
| N11 | Scoring shortlist (phase 2, flagged here because Campaign A produces ~93,000 rows): deterministic pre-rank (FTS / skills / location / seniority) and LLM scoring on ≤ 500, or raise `DREAMJOB_DEFAULT_TOKEN_BUDGET` from 2 M to ≥ 100 M. | one `complete_json` per opportunity ≈ 10k tokens; 10,000 rows ≈ 100 M tokens ≈ EUR 36; the 2 M default degrades after ~200 | the corpus is not throttled to 200 scored rows | small | none |

Sequence: C1–C9 (half a day) → Campaign B as the acceptance test → N1 + N2 + N7 → Campaign A serial-interleaved (fits 4 h only with interleaving; otherwise split EURES across two runs) → N5 + N3 + N8 → N4 + N6 → nightly refresh job.

---

## 6. What we should not do

The product's legal posture (FR-182 robots compliance, IR-101 platform terms, NFR-402 provenance) is the asset; none of the following is worth a single company.

1. **Do not touch Indeed, LinkedIn, StepStone or Jobat.** Indeed is `prohibited` in the catalogue; LinkedIn scraping is IR-101; StepStone's terms restrict automated access; Jobat answers 403 to every non-browser request (60/60) behind Cloudflare. Circumventing a 403 with a browser profile, User-Agent spoofing, or IP rotation is exactly the behaviour the product promises not to engage in. Yield forgone: 0 compliant rows, so nothing is forgone.

   **This bans fetching those sites; it does not ban the alert-mail route.** LinkedIn, ictjob.be, Glassdoor and Jobat all offer job-alert e-mails, and `backend/dreamjob/mail/job_alerts.py` reads the ones the seeker already receives, in her own mailbox, over read-only IMAP or `gmail.readonly`. It never visits any of those sites: the module makes no HTTP call of any kind. (Its direct imports contain no HTTP client, but `httpx` is reachable through `adapters.base` -> `egress.client`, so the claim that holds is the one about calls, not about imports - there is no egress name bound in the module and the two functions it calls downstream, `watchlist.add_to_ranked_list` and `notify_added`, are database-only.) That is the lawful alternative this rule exists to push us towards, not a loophole in it.

   Jobat is the sharp case and worth stating. Every link in a Jobat alert is an opaque `optiext/optiextension.dll?ID=<blob>` redirector whose target cannot be recovered without following it — which would be a request to a Jobat host, i.e. the thing forbidden above. So `alert.jobat` stores title, employer and location and **no URL**, marked `application_channel = "manual_search"`; the seeker looks the offer up herself, or the employer name can be taken through `company_domains` -> `adapters/ats/detect` -> `vacancy_refresh`, which returns the posting with a real URL from the vendor's own keyless API. Two honest limits on that second route: the alert pass does not start it (it calls only `watchlist.add_to_ranked_list` and `notify_added`; `vacancy_refresh` reads companies that already hold a stored ATS board), and as of 2026-10-06 all 13 companies behind the Jobat leads have `domain`, `ats_vendor`, `ats_slug` and `careers_url` NULL, so it currently covers none of them. At least 7 of the 13 are staffing agencies, where the board carries the agency's own advert rather than the Jobat one. Note also that `browser/pacing.py::detect_challenge` (FR-203, RK-01) already treats `cf-turnstile` as a stop signal, so driving a headful browser at Jobat's Cloudflare 403 would require weakening RK-01.
2. **Do not call api.smartrecruiters.com.** Its robots.txt allows only `LinkedInBot` (`User-agent: * / Disallow: /`). 1,037 boards and ~96,000 jobs are behind it and they stay there unless someone writes an HTML adapter against `jobs.smartrecruiters.com/{slug}`, whose robots.txt returns 404 (permitted). Do not send a LinkedInBot User-Agent.
3. **Do not fetch Common Crawl, VDAB's `/api/vindeenjob/`, Le Forem's `/recherche-offres/`, Remotive's `/api/*`, or the WTTJ Algolia backend through the egress layer.** All are robots-disallowed (or, for WTTJ, keyed). Common Crawl is the one exception in spirit — an open dataset published for reuse — and the plan uses it, but only in an operator-run importer with the terms of use cited, never inside a campaign. VDAB's open data and werk.nl's sitemap redirect to logins; do not create accounts to scrape behind them.
4. **Do not ignore europa.eu's `Crawl-delay: 10`.** DomainLimiter does not read it today and would hit EURES at 0.5 rps. The compliant rate costs 26 extra minutes on Campaign B and makes EURES the long pole of Campaign A; that is the price of being the one client the EU's PES aggregator has no reason to block. Add Crawl-delay support before the EURES adapter goes live.
5. **Do not raise `per_domain_rps` above 1.0 for JSON APIs or above 0.5 for HTML sites, and do not fan out per-tenant vendors without a vendor-group bucket.** Recruitee returned 429 on 11 of 56 requests at concurrency 10 across tenants; a vendor sees one client IP regardless of how many subdomains it has. A blocked IP loses every board of that vendor for everyone.
6. **Do not use `detect_ats` as the bulk discovery route.** 15,000 requests to 7,500 company home pages for ~500 boards is the wrong direction (board → company, then match to registers by name, is 1,000× cheaper) and it looks like a crawler to every small-business website it touches.
7. **Do not let the LLM invent companies or board slugs.** Prompt rule 3 ("leave the list empty rather than inventing one") is correct; a hallucinated slug is a 404 that gets stored, re-probed and counted. Discovery must be deterministic and evidenced (registry, EURES, user directives).
8. **Do not re-download 14,600 boards every night.** 3–5 MB Greenhouse boards with `content=true` are ~3 GB per pass; ETags make unchanged boards free. Fetch `content=true` only when the list-level ETag changed, and fetch Greenhouse board names once per 90 days, not per campaign.
9. **Do not run LLM extraction over the gathered pages.** 10,000 pages × 2,500 tokens = 25 M tokens (EUR 6.6) for nothing: ATS, EURES and Actiris parse deterministically. Keep `llm_fallback = False` and cost them at 0 in `estimate()`.
10. **Do not treat overshoot as a reason to fetch fewer jobs per company.** Taking one job per board to "hit 10,000" would throw away 90% of the data for free requests already spent. Gather whole boards; let scoring filter.

---

## 7. Recommended caps

Paste into `backend/dreamjob/pipeline/planning.py` in place of lines 49–67:

```python
# Per-source ceilings applied before the user ever sees the plan (FR-186).
#
# A "page" means one whole board for ATS adapters (pagination=False in the
# catalogue), 50 rows for board.eures, 50 offers for board.actiris and
# 250 rows for board.arbeitnow. max_pages_per_source applies per plan item;
# ATS items are one page each, so it bounds only the paginated boards.
#
# Sizing (Campaign A, docs/Data_Gathering_Plan.md §2.4): ~6,900 ATS boards
# + 600 EURES pages + ~20 Actiris pages ≈ 7,500 pages; 10,000 leaves room
# for a full-registry refresh tranche. max_companies is the 7,500 target
# plus slack and is only meaningful once collection writes company rows.
DEFAULT_CAPS: dict[str, int] = {
    "max_pages": 10_000,            # was 200
    "max_pages_per_source": 20,     # unchanged; partition EURES by NUTS-1 x NACE x period instead
    "max_companies": 8_000,         # was 200
    "max_people": 100,              # unchanged; not exercised in the first phase
    "max_duration_seconds": 4 * 3600,  # unchanged (NFR-103)
}

# FR-165: the network crawl is slow and intrusive, so it is capped hard.
LINKEDIN_NETWORK_ADAPTER = "linkedin_network"
DEFAULT_NETWORK_CAPS = {"max_profiles": 50, "max_companies": 40}  # unchanged

# Cost model for the extraction the collected pages will trigger (FR-163).
# Deterministic adapters parse without an LLM (llm_fallback = False) and
# answer in 0.14-1.1 s; costing them at 8 s and 2,900 tokens per page made a
# 7,500-board plan display 16.7 h and 21.75 M tokens.
PLAN_MAX_TOKENS = 6_000
PLAN_BATCH_SIZE = 3
TOKENS_PER_PAGE_IN = 2_500
TOKENS_PER_PAGE_OUT = 400
SECONDS_PER_PAGE_OVERHEAD = 4          # HTML and browser sources
SECONDS_PER_PAGE_OVERHEAD_API = 1      # JSON APIs (ATS, EURES, Arbeitnow)
DETERMINISTIC_ADAPTER_PREFIXES = (     # 0 LLM tokens in estimate()
    "ats.", "board.eures", "board.actiris", "board.arbeitnow",
)
```

And in `estimate()`:

```python
    deterministic = str(entry.get("adapter_key") or "").startswith(DETERMINISTIC_ADAPTER_PREFIXES)
    overhead = SECONDS_PER_PAGE_OVERHEAD_API if entry.get("access_method") == "api" else SECONDS_PER_PAGE_OVERHEAD
    per_page = (1.0 / rps if rps > 0 else 2.0) + overhead
    ...
    llm_cost = 0.0 if deterministic else pages * (...)
```

Companion constants (one line each):

```python
# backend/dreamjob/adapters/jobboards/eures.py
DEFAULT_API_BASE = "https://europa.eu/eures/api/jv-searchengine/public"   # was .../eures-apps/searchengine/page (404)
# ... and in plan(): "fetch_details": False                                # description is inline; jv-details is 404

# backend/dreamjob/pipeline/dedup.py
VACANCY_MATCH_THRESHOLD = 0.92   # was 0.86; with the location veto (N6) retention on real boards is >= 95%

# backend/dreamjob/pipeline/knowledge_base.py
DEFAULT_STALENESS_DAYS = {
    "vacancy": 7, "company": 90, "contact": 180, "financial_year": 365,
    "hiring_signal": 30, "competitor_link": 180, "event": 30,
    "board_registry": 30,        # new: re-verify slug liveness monthly
}
```

```ini
# .env — unchanged values restated so nobody "fixes" them upward
DREAMJOB_PER_DOMAIN_RPS=0.5
DREAMJOB_HTTP_MAX_CONCURRENCY=20
DREAMJOB_HTTP_CACHE_TTL_SECONDS=86400
# new (take effect with N7 / N4)
DREAMJOB_NEGATIVE_CACHE_TTL_SECONDS=604800      # 404/410 on board and feed probes; 21600 for 429/5xx
DREAMJOB_RAW_DOCUMENT_RETENTION_DAYS=30
DREAMJOB_HONOUR_CRAWL_DELAY=true                # europa.eu 10 s, api.lever.co 1 s
```

Note on `max_duration_seconds`: it is left at 4 h, but Campaign A only fits inside it with domain-interleaved or parallel collection (N5). Until N5 lands, either split EURES across two runs or accept that a serial run will stop on `max_duration_seconds` after ~4,000 boards — which still yields ~6,500 companies.

---

## Appendix A — disagreements between the evaluations, resolved

| Question | Views | Resolution |
|---|---|---|
| Jobs per ATS board | 15 mean / 7 median (random registry sample, n≈250) vs 77 mean / 30–54 median (50 guessed slugs of known companies) | Both correct for their population. **15 for registry boards, ~40 for curated Benelux boards.** Guessed slugs select for large, well-known employers. |
| Are 7,500 ATS-board companies reachable? | "No directory exists, slug guessing hits 16%" vs "15,922 slugs from Common Crawl / Wayback in 65 requests" | **Reachable globally** via the registry (14,600 live boards); **not reachable as Benelux-only** boards (900–1,500 exist). The first view did not know about the URL-index route. |
| Time for 7,500 boards | 6.3 h (2 s floor, full registry, serial), 7.1 h (3.4 s measured per board, serial), 4 h 10 min (7,499 requests to one host) | All consistent: serial cost is 2.0–3.4 s per board depending on body size; per-tenant vendors are latency-bound until a vendor-group bucket is added. **Parallel buckets make wall-clock the longest bucket: 2.2 h for the full registry, 100 min for Campaign A.** |
| EURES distinct-employer rate | 26% (200 rows) vs 20.5% (1,000 rows) then ~1% on deep pages | **20% on the first 10–20 pages of a partition**, unmeasured under partitioning; hence the 3,000–5,000 range for 600 requests. One partitioned run settles it. |
| europa.eu rate | planned at 0.5 rps (7 min for 10,000 jobs) vs `Crawl-delay: 10` observed | **Honour 10 s** (33 min). The path is permitted; the delay is the site's stated wish and the product's posture is to respect it. |
| Cap values | `max_pages 20,000 / max_companies 10,000` vs tiers 150/600/1,500 and 8,000 | **10,000 / 8,000.** 20,000 counted each Actiris offer as a page; at 50 offers per page it is not needed. Tiers (light / standard / broad) are a good UI feature later, not a default. |
| `max_pages_per_source` | 200 for API sources vs keep 20 and partition | **Keep 20 and partition** (N2) so one cap keeps one meaning; 126 possible EURES partitions × 20 pages × 50 rows is 126,000 rows of capacity. |
| ATS host rate | catalogue 1.0 rps vs `.env` 0.5 | Plan at **0.5**. Wiring the catalogue value into the limiter, capped at 1.0 and only where robots.txt states no delay, is a defensible follow-up that halves ATS bucket times. |

## Appendix B — numbers that would change the plan if wrong

1. Distinct-employer rate of a partitioned EURES sweep (drives Campaign C; range 5,000–8,000). Settle with one 600-request run.
2. EU/Benelux share of Workable and Teamtailor boards (unmeasured; assumed high). Settle by location-tagging the first 1,000 Workable boards fetched.
3. Workday jobs per board (mean 98, median 33). Immaterial to companies; affects disk.
4. Actiris distinct-employer rate beyond the 25 sampled offers (assumed 80%). Settle in the first 1,000.
5. Day-to-day ETag churn on ATS boards (drives the byte cost of weekly revalidation; assumed mostly unchanged).
6. Whether a company with an empty board should count toward `max_companies` (this plan counts it: an identity is an identity).

## Appendix C — evidence

Scratchpad: `/private/tmp/claude-502/-Users-nstephane-Dev-AI-Data-Science-training-dreamjob/ba765716-77c6-4e31-8026-66b71a865222/scratchpad/` — `cc_slugs.py`, `wayback.py`, `hn.py`, `verify_boards.py`, `verify_wk_tt.py`, `workday_wb.py`, `bridge.py`, `eures_density.py`, `actiris_sample.py`, `aggregators.py`, `arbeitnow2.py`, `sqlite_bench.py`, `probe_robots.py`, `probe_eures_api*.py`, `probe_eures_buckets.py`, `probe_eures_depth.py`, `probe_boards.py`, `probe_tenant_hosts.py`, `dedup_test.py`, `dedup_real.py`, `dedup_fix.py`, `bench_e2e.py`, `exp1_http_cache.py`, `exp2_raw_document.py`, `exp3_kb_reuse.py`, `exp5_scale.py`; results `verify_boards_result.json`, `bridge_result.json`, `cc_slugs_*.json`, `wb_slugs_*.json`, `hn_slugs.json`.

Code referenced: `backend/dreamjob/pipeline/planning.py` (`DEFAULT_CAPS` :50, `estimate()` :564, `resolve_caps()` :647, per-source fallback :700), `backend/dreamjob/pipeline/collection.py` (`_cap_hit` :122, `_pages_for` :156, serial loop :274), `backend/dreamjob/pipeline/knowledge_base.py` (`DEFAULT_STALENESS_DAYS` :39, `assess_reuse` :558), `backend/dreamjob/pipeline/dedup.py` (:263, :268–274), `backend/dreamjob/adapters/ats/common.py` (`slug_of` :149), `backend/dreamjob/adapters/jobboards/eures.py` (:62, :106), `backend/dreamjob/egress/client.py` (`DomainLimiter` :86–102, `_cache_lookup` :254, request built without conditional headers, limiter built from the global rps :134), `backend/dreamjob/llm/prompts/campaign_plan.md` (rule 3), `tests/unit/test_ats_live_fixtures.py:175` (xfail for single-slug reads).
