"""Central configuration (CR-407..410, NFR-306).

All runtime configuration is read from the environment / .env once and exposed
as a single frozen ``Settings`` object.  Nothing in the codebase reads
``os.environ`` directly; that keeps the deployment surface auditable and lets
the administration screens (FR-362, FR-363) show exactly what is in force.
"""

from __future__ import annotations

import base64
from datetime import time
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core --------------------------------------------------------------
    env: str = Field("development", alias="DREAMJOB_ENV")
    host: str = Field("127.0.0.1", alias="DREAMJOB_HOST")
    port: int = Field(8000, alias="DREAMJOB_PORT")
    data_dir: Path = Field(Path("data"), alias="DREAMJOB_DATA_DIR")
    db_path: Path = Field(Path("data/dreamjob.db"), alias="DREAMJOB_DB_PATH")

    master_key: str = Field("", alias="DREAMJOB_MASTER_KEY")
    session_secret: str = Field("", alias="DREAMJOB_SESSION_SECRET")

    # --- LLM (CR-409) ------------------------------------------------------
    llm_provider: str = Field("deepseek", alias="DREAMJOB_LLM_PROVIDER")
    deepseek_api_key: str = Field("", alias="DEEPSEEK_API_KEY")
    # Extra keys for the same OpenAI-compatible endpoint.  A free tier (Groq)
    # rate-limits each key per minute, so the client moves to the next key on
    # a 429 instead of sleeping; see ``deepseek_api_keys``.
    deepseek_api_key_2: str = Field("", alias="DEEPSEEK_API_KEY_2")
    deepseek_api_key_3: str = Field("", alias="DEEPSEEK_API_KEY_3")
    deepseek_api_key_4: str = Field("", alias="DEEPSEEK_API_KEY_4")
    deepseek_api_key_5: str = Field("", alias="DEEPSEEK_API_KEY_5")
    # A second OpenAI-compatible endpoint tried only when the main one still
    # fails after all its keys and retries (e.g. Groq first, DeepSeek behind
    # it).  No key = no fallback.  deepseek-chat for both roles by default: it
    # is the cheaper model, and the fallback exists to keep a call alive.
    fallback_llm_base_url: str = Field(
        "https://api.deepseek.com", alias="DREAMJOB_FALLBACK_LLM_BASE_URL"
    )
    fallback_llm_api_key: str = Field("", alias="DREAMJOB_FALLBACK_LLM_API_KEY")
    fallback_llm_model_cheap: str = Field("deepseek-chat", alias="DREAMJOB_FALLBACK_LLM_MODEL_CHEAP")
    fallback_llm_model_strong: str = Field(
        "deepseek-chat", alias="DREAMJOB_FALLBACK_LLM_MODEL_STRONG"
    )
    deepseek_base_url: str = Field("https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    llm_model_cheap: str = Field("deepseek-chat", alias="DREAMJOB_LLM_MODEL_CHEAP")
    llm_model_strong: str = Field("deepseek-reasoner", alias="DREAMJOB_LLM_MODEL_STRONG")

    # NFR-306: privacy-sensitive tasks may be routed to a local model.
    local_llm_base_url: str = Field("", alias="DREAMJOB_LOCAL_LLM_BASE_URL")
    local_llm_model: str = Field("", alias="DREAMJOB_LOCAL_LLM_MODEL")
    local_llm_api_key: str = Field("not-needed", alias="DREAMJOB_LOCAL_LLM_API_KEY")
    local_llm_tasks: str = Field("", alias="DREAMJOB_LOCAL_LLM_TASKS")

    # Optional embeddings model for the semantic index (FR-261).  Empty means
    # no semantic index: the feature is inert rather than broken.
    embeddings_model: str = Field("", alias="DREAMJOB_EMBEDDINGS_MODEL")
    embeddings_base_url: str = Field("", alias="DREAMJOB_EMBEDDINGS_BASE_URL")

    default_token_budget: int = Field(2_000_000, alias="DREAMJOB_DEFAULT_TOKEN_BUDGET")
    # How many opportunities of a campaign earn an LLM scoring call (NFR-104;
    # docs/Data_Gathering_Plan.md N11).  A campaign can produce ~93,000
    # opportunities and one call is ~10k tokens, so the whole corpus is ranked
    # deterministically and only this many rows are also read by the model.
    llm_scored_limit: int = Field(500, alias="DREAMJOB_LLM_SCORED_LIMIT")
    llm_cost_per_1m_input_eur: float = Field(0.25, alias="DREAMJOB_LLM_COST_PER_1M_INPUT_EUR")
    llm_cost_per_1m_output_eur: float = Field(1.00, alias="DREAMJOB_LLM_COST_PER_1M_OUTPUT_EUR")

    # --- Mail (FR-325) -----------------------------------------------------
    mail_backend: str = Field("resend", alias="DREAMJOB_MAIL_BACKEND")
    mail_from: str = Field("", alias="DREAMJOB_MAIL_FROM")
    mail_from_name: str = Field("", alias="DREAMJOB_MAIL_FROM_NAME")
    resend_api_key: str = Field("", alias="RESEND_API_KEY")
    gmail_client_id: str = Field("", alias="GMAIL_CLIENT_ID")
    gmail_client_secret: str = Field("", alias="GMAIL_CLIENT_SECRET")
    gmail_redirect_uri: str = Field(
        "http://127.0.0.1:8000/api/mail/gmail/callback", alias="GMAIL_REDIRECT_URI"
    )
    imap_host: str = Field("imap.gmail.com", alias="DREAMJOB_IMAP_HOST")
    imap_port: int = Field(993, alias="DREAMJOB_IMAP_PORT")

    # RK-05: the transport guard.  True means every send runs the whole path -
    # recipient, guard rails, MIME with the CV attached - and stops one step
    # short of the wire, writing the assembled message to data/generated/
    # dry_run/ instead.  It defaults to true so that no message can leave by
    # accident; arming a real send is a deliberate change of this value plus a
    # restart.  Enforced in dreamjob.mail.dry_run, never in the interface.
    mail_dry_run: bool = Field(True, alias="DREAMJOB_MAIL_DRY_RUN")

    send_daily_cap: int = Field(25, alias="DREAMJOB_SEND_DAILY_CAP")
    send_min_interval_seconds: int = Field(90, alias="DREAMJOB_SEND_MIN_INTERVAL_SECONDS")
    send_window_start: time = Field(time(8, 30), alias="DREAMJOB_SEND_WINDOW_START")
    send_window_end: time = Field(time(17, 30), alias="DREAMJOB_SEND_WINDOW_END")

    # --- Egress (FR-182, IR-102) -------------------------------------------
    user_agent: str = Field(
        "DreamJobBot/0.1 (+https://stepvda.net/dreamjob; contact stephane@stepvda.com)",
        alias="DREAMJOB_USER_AGENT",
    )
    http_cache_ttl_seconds: int = Field(86_400, alias="DREAMJOB_HTTP_CACHE_TTL_SECONDS")
    # Vacancy sources (ATS boards, job boards) change within the hour while
    # registries and filings change yearly, so one TTL for both either serves
    # day-old postings or re-reads accounts for nothing.  Vacancy fetches pass
    # this as ``max_age``; an unchanged board revalidates as a 304.
    vacancy_cache_ttl_seconds: int = Field(3_600, alias="DREAMJOB_VACANCY_CACHE_TTL_SECONDS")
    per_domain_rps: float = Field(0.5, alias="DREAMJOB_PER_DOMAIN_RPS")
    http_max_concurrency: int = Field(20, alias="DREAMJOB_HTTP_MAX_CONCURRENCY")
    # NFR-102: how many background jobs may hold a worker thread at once, and
    # how many I/O threads each one may spawn for its own ``to_thread`` calls.
    # Both are bounded on purpose: the alternative to a starved event loop is
    # not an unbounded thread count, and CR-408 still allows only one writer.
    job_pool_size: int = Field(4, alias="DREAMJOB_JOB_POOL_SIZE")

    # Continuous monitoring (FR-401, FR-403, NFR-303, FR-364).  The scheduler
    # implements watchlist checks, digests, reply polling, follow-ups, contact
    # retention and prompt redaction, but nothing ever started it - so on a
    # running installation every one of those stayed dormant.  It is on by
    # default and can be turned off for an external `--once` cron, or in tests.
    scheduler_enabled: bool = Field(True, alias="DREAMJOB_SCHEDULER_ENABLED")
    scheduler_tick_seconds: int = Field(60, alias="DREAMJOB_SCHEDULER_TICK_SECONDS")
    # How often the scheduler re-reads the ATS boards of companies already in
    # someone's ranked list (pipeline/vacancy_refresh.py); 0 turns it off.
    vacancy_refresh_seconds: int = Field(600, alias="DREAMJOB_VACANCY_REFRESH_SECONDS")
    # How often connected Gmail mailboxes are read for LinkedIn / ictjob.be
    # job-alert e-mails (mail/job_alerts.py); 0 turns it off.
    job_alerts_seconds: int = Field(900, alias="DREAMJOB_JOB_ALERTS_SECONDS")
    # A non-Gmail mailbox read for job alerts over IMAP with an app password
    # (Yahoo: Account security -> Generate app password).  The alerts go to the
    # account whose login e-mail is ``alerts_imap_user``.  Empty = off.
    alerts_imap_host: str = Field("imap.mail.yahoo.com", alias="DREAMJOB_ALERTS_IMAP_HOST")
    alerts_imap_user: str = Field("", alias="DREAMJOB_ALERTS_IMAP_USER")
    alerts_imap_password: str = Field("", alias="DREAMJOB_ALERTS_IMAP_PASSWORD")
    job_io_threads: int = Field(8, alias="DREAMJOB_JOB_IO_THREADS")
    # How long a writer waits for the single writer (CR-408) before giving up.
    # A request is impatient because a person is waiting and an error beats a
    # page that never loads; a job is patient because its work still has to
    # happen.  Both live here rather than in ``os.environ`` so that setting
    # them in ``.env`` works: pydantic-settings reads that file itself and
    # never exports it, so a module reading ``os.environ`` at import time
    # would silently keep the default.
    write_wait_seconds: float = Field(15.0, alias="DREAMJOB_WRITE_WAIT_SECONDS")
    bulk_write_wait_seconds: float = Field(600.0, alias="DREAMJOB_BULK_WRITE_WAIT_SECONDS")
    respect_robots: bool = Field(True, alias="DREAMJOB_RESPECT_ROBOTS")

    # How long a failure is believed, so a gone page is not re-probed on every
    # pass (FR-182; docs/Data_Gathering_Plan.md items N4/N7).  404 and 410 get
    # this value; 429 and 5xx are re-probed after six hours, because "come back
    # later" is what they mean.
    negative_cache_ttl_seconds: int = Field(604_800, alias="DREAMJOB_NEGATIVE_CACHE_TTL_SECONDS")
    # How long a stored page is kept before the pruner may reclaim it (DR-102):
    # a weekly revalidation of 6,900 boards orphans ~160 MB a week otherwise.
    raw_document_retention_days: int = Field(30, alias="DREAMJOB_RAW_DOCUMENT_RETENTION_DAYS")
    # robots.txt says how fast as well as whether (FR-182): europa.eu asks for
    # 10 s, api.lever.co for 1 s.  Off is for tests that must not sleep.
    honour_crawl_delay: bool = Field(True, alias="DREAMJOB_HONOUR_CRAWL_DELAY")
    # Adzuna's job-search API is free but keyed (developer.adzuna.com).  With
    # either value empty, board.adzuna stays catalogued but inert: the planner
    # leaves it out with the reason, as it does a registry without its key
    # (FR-245).  The pair is sent as query parameters, never in the stored URL.
    adzuna_app_id: str = Field("", alias="DREAMJOB_ADZUNA_APP_ID")
    adzuna_app_key: str = Field("", alias="DREAMJOB_ADZUNA_APP_KEY")

    # --- Browser automation (FR-201..208) ----------------------------------
    cdp_url: str = Field("http://127.0.0.1:9222", alias="DREAMJOB_CDP_URL")
    browser_profile_dir: Path = Field(Path(".chrome-profile"), alias="DREAMJOB_BROWSER_PROFILE_DIR")
    browser_min_delay_ms: int = Field(2500, alias="DREAMJOB_BROWSER_MIN_DELAY_MS")
    browser_max_delay_ms: int = Field(6000, alias="DREAMJOB_BROWSER_MAX_DELAY_MS")

    # --- Observability (NFR-701, NFR-702) ----------------------------------
    # Everything the operator needs to read after the fact lands under
    # ``log_dir``.  Rotation is sized so a week of ordinary use fits on disk
    # without anyone having to remember to prune it.
    log_dir: Path = Field(Path("logs"), alias="DREAMJOB_LOG_DIR")
    log_level: str = Field("INFO", alias="DREAMJOB_LOG_LEVEL")
    log_format: str = Field("text", alias="DREAMJOB_LOG_FORMAT")
    log_max_mb: int = Field(10, alias="DREAMJOB_LOG_MAX_MB")
    log_backups: int = Field(5, alias="DREAMJOB_LOG_BACKUPS")
    log_slow_request_ms: int = Field(1500, alias="DREAMJOB_LOG_SLOW_REQUEST_MS")

    # Statement tracing costs a Python callback per statement, so it is a flag:
    # unset means on outside production.  The slow-query threshold is the one
    # number that turns logs/database.log into a missing-index report.
    log_sql: bool | None = Field(None, alias="DREAMJOB_LOG_SQL")
    log_slow_query_ms: int = Field(200, alias="DREAMJOB_LOG_SLOW_QUERY_MS")

    # --- Misc --------------------------------------------------------------
    geocoder_url: str = Field("https://nominatim.openstreetmap.org", alias="DREAMJOB_GEOCODER_URL")
    google_calendar_client_id: str = Field("", alias="GOOGLE_CALENDAR_CLIENT_ID")
    google_calendar_client_secret: str = Field("", alias="GOOGLE_CALENDAR_CLIENT_SECRET")

    @field_validator("send_window_start", "send_window_end", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str) and ":" in v:
            hh, mm = v.split(":")[:2]
            return time(int(hh), int(mm))
        return v

    # --- Derived helpers ---------------------------------------------------
    @property
    def deepseek_api_keys(self) -> list[str]:
        """Every configured key for the main LLM endpoint, in order, without repeats."""
        keys: list[str] = []
        for key in (self.deepseek_api_key, self.deepseek_api_key_2, self.deepseek_api_key_3,
                    self.deepseek_api_key_4, self.deepseek_api_key_5):
            key = (key or "").strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    @property
    def abs_data_dir(self) -> Path:
        p = self.data_dir
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def abs_db_path(self) -> Path:
        p = self.db_path
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def abs_log_dir(self) -> Path:
        p = self.log_dir
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def raw_dir(self) -> Path:
        return self.abs_data_dir / "raw"

    @property
    def generated_dir(self) -> Path:
        return self.abs_data_dir / "generated"

    @property
    def uploads_dir(self) -> Path:
        return self.abs_data_dir / "uploads"

    @property
    def local_llm_task_set(self) -> set[str]:
        return {t.strip() for t in self.local_llm_tasks.split(",") if t.strip()}

    def master_key_bytes(self) -> bytes:
        """32-byte key for envelope encryption (NFR-201).

        A missing key is tolerated in development so the app boots for a first
        look, but every encrypt call records that data is unprotected.
        """
        if not self.master_key:
            return b""
        raw = base64.urlsafe_b64decode(self.master_key.encode())
        if len(raw) != 32:
            raise ValueError("DREAMJOB_MASTER_KEY must decode to exactly 32 bytes")
        return raw

    def ensure_dirs(self) -> None:
        for d in (
            self.abs_data_dir,
            self.raw_dir,
            self.generated_dir,
            self.uploads_dir,
            self.abs_log_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.abs_db_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
