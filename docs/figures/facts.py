"""Measured facts about the built system.

Every number that appears in a figure comes from here, and every number here was
measured rather than estimated.  Keeping them in one module means a figure
cannot quietly drift from the system, and it means a reader who doubts a chart
can find out exactly what produced it.

Regenerate the counts with ``scripts/traceability.py`` and the shell one-liners
noted beside each block.
"""

from __future__ import annotations

# --- Codebase ---------------------------------------------------------------
# find backend/dreamjob -name '*.py' -not -path '*__pycache__*' | xargs wc -l

CODEBASE = {
    "backend_files": 227,
    "backend_lines": 120_881,
    "frontend_files": 140,
    "frontend_lines": 43_932,
    "test_files": 114,
    "test_lines": 61_396,
    "tests_collected": 2_061,
    "tests_passing": 2_055,
    "api_routes": 422,
    "api_paths": 351,
    "db_tables": 88,
    "db_indexes": 158,
    "db_fts": 2,
    "migrations": 43,
    "prompt_templates": 24,
}

# Lines per backend package.
PACKAGE_LINES = {
    "pipeline": 48_100,
    "db/repositories": 15_088,
    "adapters": 11_793,
    "api/routers": 11_543,
    "documents": 7_088,
    "postapp": 5_752,
    "mail": 3_903,
    "intelligence": 3_969,
    "browser": 3_786,
    "monitoring": 1_862,
    "observability": 1_627,
    "egress": 1_137,
    "security": 1_085,
    "llm": 1_270,
    "exporting": 798,
    "jobs": 729,
}

# --- Requirement coverage ---------------------------------------------------
# python3 scripts/traceability.py

COVERAGE = {
    "total": 157,
    "cited": 156,
    "by_priority": {          # (cited, total)
        "Must": (103, 104),
        "Should": (44, 44),
        "Could": (9, 9),
    },
    "uncited": [
        ("NFR-304", "M", "A DPIA shall be produced before production use"),
    ],
}

# --- Source adapters --------------------------------------------------------

ADAPTERS = {
    "ATS": 9,
    "Job boards": 9,
    "Registries": 5,
    "News / events": 3,
    "Directories": 1,
    "Website crawler": 1,
    "Compensation": 1,
}

ADAPTER_NAMES = {
    "ATS": ["Greenhouse", "Lever", "SmartRecruiters", "Ashby", "Recruitee",
            "Personio", "Teamtailor", "Workable", "Workday"],
    "Job boards": ["EURES", "VDAB", "Actiris", "Jobat", "StepStone", "Indeed",
                   "Arbeitnow", "Welcome to the Jungle", "generic HTML"],
    "Registries": ["NBB", "KBO/BCE", "Companies House", "KvK", "SEC EDGAR"],
    "News / events": ["company newsroom / RSS", "conference and community calendars",
                      "Meetup and Eventbrite"],
    "Directories": ["OpenCorporates"],
    "Website crawler": ["company website"],
    "Compensation": ["Eurostat Structure of Earnings Survey"],
}

# --- Frontend bundle --------------------------------------------------------
# npm run build, before and after route-level code splitting.

BUNDLE = {
    "before_kb": 858.25,
    "before_gzip_kb": 243.13,
    "after_initial_kb": 312.4,
    "after_initial_gzip_kb": 105.5,
    "largest_page_kb": 88.2,       # AdminPage
    "smallest_page_kb": 7.6,       # HomePage
    "lazy_chunks": 33,
}

# --- Accessibility ----------------------------------------------------------
# Measured contrast ratios of each phase hue. WCAG AA for small text is 4.5:1.

CONTRAST_LIGHT = {          # (on white surface, on its own soft tint)
    "Profile": (5.37, 4.63),
    "Plan": (5.19, 4.56),
    "Discover": (5.13, 4.51),
    "Apply": (5.05, 4.52),
    "Follow up": (5.25, 4.55),
    "System": (5.78, 5.16),
}

CONTRAST_DARK = {
    "Profile": (6.39, 5.92),
    "Plan": (7.10, 6.25),
    "Discover": (8.40, 6.99),
    "Apply": (8.51, 7.49),
    "Follow up": (7.19, 6.63),
    "System": (6.12, 5.44),
}

WCAG_AA_SMALL = 4.5
WCAG_AA_LARGE = 3.0

# --- Verified against the product owner's real documents --------------------
# docs/Profile.pdf (LinkedIn export) and docs/CV - Stephane van der Aa.docx

REAL_INTAKE = {
    "experience_entries": 8,
    "top_skills": 27,
    "languages": 4,
    "publications": 3,
    "summary_chars": 1_163,
    "normalised_skills": 52,
    "conflicts": 18,
    "photo_extracted": True,
}

# The 18 conflicts, grouped by what disagreed.
CONFLICT_KINDS = {
    "Role dates": 7,
    "Employer name": 4,
    "Job title": 3,
    "Contact details": 3,
    "Education": 1,
}

# Two of those eighteen, as (LinkedIn export, CV).  Quoted in the FDD (5.2) and
# in the docstring of backend/dreamjob/pipeline/profile_intake.py.
CONFLICT_EXAMPLES = {
    "role_dates": ("2014-11 to 2023-10", "2016 to 2020"),
    "employer": ("NGA Human Resources",
                 "Alight Solutions (formerly NGA Human Resources)"),
}

# --- Outcome learning example ----------------------------------------------
# The scenario in tests/unit/test_learning.py, which is also the product
# owner's stated case: many rejections for one kind of role.

SEGMENT_EXAMPLE = {
    "Data engineering": {"n": 8, "replies": 5},
    "Data science": {"n": 9, "replies": 1},
}

# The sample size below which redirection advice refuses to speak.
# backend/dreamjob/postapp/segments.py:42  MIN_SEGMENT_TO_ADVISE
MIN_SEGMENT_TO_ADVISE = 6

# Wilson 95% intervals for the same observed rate at different sample sizes -
# the argument for why a percentage without its n is not evidence.
SAMPLE_SIZE_DEMO = [
    # (n, successes)  all at or near 50%
    (2, 1),
    (4, 2),
    (10, 5),
    (20, 10),
    (40, 20),
    (80, 40),
]

# --- Help system ------------------------------------------------------------

HELP = {
    "screens_documented": 21,
    "glossary_terms": 142,
    "inline_tips": 305,
}

# --- Pipeline stages (specification section 2.3) ----------------------------

PHASES = [
    (1, "Profile", ["Profile intake", "Composite profile", "Dream job"]),
    (2, "Plan", ["Directives", "Campaign plan", "Collection"]),
    (3, "Discover", ["Company profiles", "Opportunities", "Ranking"]),
    (4, "Apply", ["Hiring contacts", "Documents", "Dispatch"]),
    (5, "Follow up", ["Responses", "Pipeline", "What works"]),
]

# --- Scoring ----------------------------------------------------------------

SUB_SCORES = [
    ("Profile fit", "deterministic", "skills, seniority, domain overlap"),
    ("Dream-job fit", "LLM", "0.6 semantic + 0.4 deterministic"),
    ("Directive fit", "deterministic", "location, arrangement, contract, company type"),
    ("Company", "deterministic", "trajectory, ability to pay, capacity, reviews"),
    ("Compensation", "deterministic", "estimate against the stated minimum"),
    ("Plausibility", "LLM", "every row; 1.0 when advertised"),
    ("Reachability", "deterministic", "validated contact or introduction path"),
]

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The interval used throughout the product, reproduced for the figures."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# --- Knowledge-base freshness policy (FR-343) -------------------------------
# backend/dreamjob/pipeline/knowledge_base.py :: DEFAULT_STALENESS_DAYS

STALENESS_DAYS = {
    "vacancy": 7,
    "hiring_signal": 30,
    "event": 30,
    "board_registry": 30,
    "company": 90,
    "contact": 180,
    "competitor_link": 180,
    "employer_kind": 180,
    "financial_year": 365,
    "compensation_observation": 1_460,
}

# --- Collection --------------------------------------------------------------
# grep -c 'requires_ack = True' backend/dreamjob/adapters/**/*.py
# SmartRecruiters, Indeed, StepStone, Meetup/Eventbrite ship disabled until an
# administrator acknowledges their terms (IR-101).

ADAPTERS_REQUIRING_ACK = 4

# --- Financial analysis (FR-241..FR-246) -------------------------------------
# backend/dreamjob/pipeline/financial.py :: YearFigures, score(),
# ESTIMATED_SCORE_CEILING

FINANCIAL_YEARS = 5

# The two 0-100 scores and the weights that produce them.
ABILITY_TO_PAY_WEIGHTS = {
    "Cost per FTE": 0.35,
    "EBIT margin": 0.25,
    "Equity / assets": 0.20,
    "Current ratio": 0.20,
}

INVESTMENT_CAPACITY_WEIGHTS = {
    "Revenue CAGR": 0.22,
    "Cash runway": 0.22,
    "Headcount CAGR": 0.18,
    "EBITDA margin": 0.18,
    "Debt / equity": 0.12,
    "Capex / revenue": 0.08,
}

# FR-245: a profile built from secondary signals rather than filings is marked
# estimated, and neither score may claim more than this.
ESTIMATED_SCORE_CEILING = 55

# --- Outcome segmentation (FDD 9.3) -----------------------------------------

SEGMENT_DIMENSIONS = 9


# --- Schema scopes ----------------------------------------------------------
# Measured from the migrations and the migrated database: a table is private
# when its own definition carries a job_seeker_id column.
#
#   34 migration files, 86 application tables; plus schema_migration,
#   created by db/migrator.py, and two FTS5 virtual tables.
#
# ``restricted`` is one of the shared tables (contact), not a separate group:
# it is shared except when a row was collected through browser automation.

SCHEMA_SCOPES = {
    "private": 49,      # carry job_seeker_id, or are the seeker row itself
    "shared": 38,       # carry no link back to a job seeker
    "restricted": 1,    # contact, inside the 38 above (NFR-303)
    "ledger": 1,        # schema_migration
}

SCHEMA_PRINCIPAL = {
    # The principal tables of each scope, not all 87.
    "private": [
        "profile_version", "profile_skill", "profile_conflict", "evidence_item",
        "persona", "composite_profile", "dream_job_model",
        "directive_set", "campaign", "opportunity", "application_package",
        "dispatch", "incoming_reply", "pipeline_card", "watchlist_entry",
        "apply_selection",
    ],
    "shared": [
        "company", "vacancy", "financial_year", "financial_analysis",
        "hiring_signal", "competitor_link", "event", "raw_document",
        "provenance", "source_catalogue", "email_pattern", "board_registry",
        "company_employer_kind", "company_registry_identity",
        "compensation_observation", "embedding",
    ],
    "restricted": ["contact"],
}

# --- API surface ------------------------------------------------------------
# ls backend/dreamjob/api/routers/*.py | grep -v __init__ | wc -l

API_ROUTERS = 22
