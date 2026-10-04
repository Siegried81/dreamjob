"""Dream Job API application factory (CR-407: Python backend, React frontend).

Routers are mounted from a declared list.  Each router owns one slice of the
requirements and is independently testable; the list below doubles as a map
from URL space to specification section.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db.connection import checkpoint
from dreamjob.db.migrator import migrate
from dreamjob.observability import RequestLogMiddleware, setup_logging

log = logging.getLogger(__name__)

# (module under dreamjob.api.routers, url prefix, tag, spec sections)
ROUTERS: list[tuple[str, str, str]] = [
    ("auth",         "/api/auth",         "Authentication"),        # NFR-202
    ("autopilot",    "/api/autopilot",    "Autopilot"),             # 3-step flow
    ("profile",      "/api/profile",      "Profile"),               # FR-101..109
    ("enrichment",   "/api/enrichment",   "Enrichment"),            # FR-121..128
    ("directives",   "/api/directives",   "Directives"),            # FR-141..149
    ("campaigns",    "/api/campaigns",    "Campaigns"),             # FR-161..166, FR-185
    ("browser",      "/api/browser",      "Browser automation"),    # FR-201..208
    ("companies",    "/api/companies",    "Companies"),             # FR-221..246, FR-341..345
    ("employers",    "/api/employers",    "Employer kind"),         # FR-143, FR-341, NFR-402
    ("opportunities", "/api/opportunities", "Opportunities"),       # FR-261..285, FR-381..383
    ("contacts",     "/api/contacts",     "Contacts"),              # FR-301..306
    ("applications", "/api/applications", "Applications"),          # FR-321..331
    ("apply",        "/api/apply",        "Apply browser"),         # FR-321..325
    ("mail",         "/api/mail",         "Mail"),                  # FR-325..327
    ("pipeline",     "/api/pipeline",     "Post-application"),      # FR-421..425
    ("monitoring",   "/api/monitoring",   "Monitoring"),            # FR-401..403
    ("events", "/api/events", "Live events"),
    ("intelligence", "/api/intelligence", "Dream-job intelligence"),# FR-381..385, FR-441..444
    ("networking",   "/api/networking",   "Networking & export"),   # FR-461..463
    ("admin",        "/api/admin",        "Administration"),        # FR-361..364
    ("logs",         "/api/logs",         "Logging"),               # NFR-701, NFR-702
    ("overview",     "/api/overview",     "Journey"),               # workflow map
    ("learning",     "/api/learning",     "Responses & learning"),  # FR-285, FR-425
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_dirs()
    # First, so that everything below is already on the record (NFR-701).
    log_dir = setup_logging()
    log.info("Dream Job starting", extra={"fields": {"env": settings.env, "logs": str(log_dir)}})
    applied = migrate()
    if applied:
        log.info("Applied migrations: %s", ", ".join(applied))

    # Populate the source catalogue from the adapter registry (FR-161).
    try:
        from dreamjob.adapters import load_all  # noqa: PLC0415

        count = load_all()
        log.info("Source catalogue synchronised: %d adapters", count)
    except Exception:  # noqa: BLE001
        log.exception("Could not synchronise the source catalogue")

    # FR-181/DR-101: the shipped ATS board registry becomes rows, so the boards
    # this installation verifies, retires or resolves to a company have
    # somewhere to be recorded.  Importing never overwrites what is already
    # there - the file ships "never verified" on every row.
    try:
        from dreamjob.db.repositories import board_registry as board_repo  # noqa: PLC0415
        from dreamjob.pipeline.discovery import load_board_registry  # noqa: PLC0415

        imported = board_repo.sync_from_file(load_board_registry())
        if imported["added"]:
            log.info(
                "Board registry: %d board(s) imported, %d already known",
                imported["added"], imported["kept"],
            )
    except Exception:  # noqa: BLE001 - a registry that will not import is not a failed boot
        log.exception("Could not import the ATS board registry")

    # NFR-401: jobs interrupted by a restart are marked resumable.
    try:
        from dreamjob.jobs.runner import runner  # noqa: PLC0415

        orphans = await runner.resume_orphans()
        if orphans:
            log.info("Marked %d interrupted jobs as resumable", orphans)
            resumed = await runner.dispatch_recoverable()
            if resumed:
                log.info("Resumed %d interrupted job(s)", resumed)
    except Exception:  # noqa: BLE001
        log.exception("Could not reconcile interrupted jobs")

    # Storage hygiene: at boot nothing else holds the log open, so a TRUNCATE
    # collapses a WAL a long-running reader may have pinned for days.
    try:
        result = checkpoint("TRUNCATE")
        if result.get("checkpointed"):
            log.info("WAL checkpointed at boot: %s frame(s)", result["checkpointed"])
    except Exception:  # noqa: BLE001 - a checkpoint is never worth a failed boot
        log.exception("Could not checkpoint the WAL at boot")

    # Continuous monitoring: watchlist rechecks (FR-401), weekly digests
    # (FR-403), reply/bounce polling (FR-326), follow-ups (FR-327), contact
    # retention (NFR-303) and LLM prompt redaction (FR-364).  The scheduler
    # implemented all of these and nothing started it, so on a running
    # installation none of them had ever run.  `--once` from an external cron
    # remains available by setting DREAMJOB_SCHEDULER_ENABLED=0.
    from dreamjob.monitoring import scheduler as scheduler_mod  # noqa: PLC0415

    try:
        scheduler_mod.scheduler.tick_seconds = settings.scheduler_tick_seconds
        if scheduler_mod.maybe_autostart():
            log.info("Monitoring scheduler started")
    except Exception:  # noqa: BLE001 - monitoring must not fail the boot
        log.exception("Could not start the monitoring scheduler")

    try:
        yield
    finally:
        if scheduler_mod.scheduler.running:
            try:
                await scheduler_mod.scheduler.stop()
            except Exception:  # noqa: BLE001 - shutting down, best effort
                log.exception("Could not stop the monitoring scheduler")


_FRIENDLY_FIELDS = {
    "password": "password",
    "email": "email address",
    "display_name": "name",
    "current_password": "current password",
    "new_password": "new password",
}


def _validation_detail(exc: RequestValidationError) -> str:
    """Flatten a validation error into one human-readable sentence.

    FastAPI/Pydantic return an array of ``{loc, msg, type}`` records. Raw they
    are noise (``body.password: String should have at least 9 characters``); a
    job seeker only needs the fix. The first error is the one that matters for
    a form, and naming the field makes it actionable.
    """
    errors = exc.errors()
    if not errors:
        return "The request could not be validated"
    first = errors[0]
    loc = [str(p) for p in first.get("loc", []) if p not in ("body", "query", "path")]
    field = loc[-1] if loc else None
    msg = first.get("msg", "")
    name = _FRIENDLY_FIELDS.get(field, field.replace("_", " ") if field else "")
    cleaned = msg
    # Pydantic's phrasing is mechanical; make it a sentence a person reads.
    for prefix in ("String should have at least ", "Value should have at least "):
        if cleaned.startswith(prefix):
            cleaned = f"must be at least {cleaned[len(prefix):].split(' character')[0]} characters"
            break
    if cleaned.startswith("value is not a valid email address"):
        cleaned = "is not a valid email address"
    text = f"{name} {cleaned}" if name else cleaned or "The request could not be validated"
    return text[0].upper() + text[1:]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Dream Job",
        description="AI-assisted job discovery and application platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # So the SPA can read the id its request was logged under (NFR-701).
        expose_headers=["X-Correlation-ID"],
    )

    # Added last, so it is the outermost layer and times the whole request.
    app.add_middleware(RequestLogMiddleware)

    # Turn FastAPI's array-of-objects validation detail into one readable line.
    # Without this, a bad field surfaces as a useless "Request failed (422)".
    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request, exc):  # noqa: ANN001
        detail = _validation_detail(exc)
        return JSONResponse(status_code=422, content={"detail": detail})

    for module_name, prefix, tag in ROUTERS:
        try:
            module = importlib.import_module(f"dreamjob.api.routers.{module_name}")
        except ModuleNotFoundError:
            log.warning("Router %s is not implemented yet; skipping", module_name)
            continue
        app.include_router(module.router, prefix=prefix, tags=[tag])

    @app.get("/api/health")
    def health() -> JSONResponse:
        # Liveness only: this route is unauthenticated, so it must not disclose
        # the deployment's environment, providers, mail backend or the absolute
        # database path.
        return JSONResponse({"status": "ok"})

    # Serve the built React SPA when present (CR-407).
    dist = REPO_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        dist_root = dist.resolve()

        # ``response_model=None``: the route answers with two different
        # response classes and FastAPI would otherwise try to build a schema
        # out of the union of them.
        @app.get("/{full_path:path}", response_model=None)
        def spa(full_path: str) -> FileResponse | JSONResponse:
            """Deep links land on the SPA; everything under /api does not.

            This route matches every GET no other route claimed, and that used
            to include ``/api`` - so an endpoint that did not exist answered
            ``200 text/html`` with the SPA shell in it rather than 404.  The
            client parses that body as JSON, fails, and hands the caller a
            *string*, which reads as a successful but empty answer: the
            activity panel polled a route the server does not have every four
            seconds for the length of a run and reported "nothing recorded"
            rather than "unavailable" (FR-361).  Nothing that is not the SPA
            can be served from here, so a miss under /api is a 404 in the JSON
            shape the rest of the API answers in.

            The path is also resolved against ``dist`` before it is served.
            ``dist / full_path`` follows ``..`` out of the directory, and the
            repository root above it holds ``.env`` (NFR-201).
            """
            if full_path == "api" or full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"detail": "Not Found"})
            if full_path:
                candidate = (dist / full_path).resolve()
                if candidate.is_file() and candidate.is_relative_to(dist_root):
                    return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


app = create_app()


def main() -> None:  # pragma: no cover
    import uvicorn

    s = get_settings()
    setup_logging()
    uvicorn.run("dreamjob.main:app", host=s.host, port=s.port, reload=s.env == "development")


if __name__ == "__main__":  # pragma: no cover
    main()
