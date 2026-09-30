"""Compose Dream_Job_Walkthrough_and_Architecture_v2.mp4.

Interleaves the v2 slides with demo clips cut from the recorded tour, all under
one Microsoft Brian (en-US) narration. Each beat is encoded to identical
parameters and then concatenated, so the result is a single clean video.

Run:  python build_video.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import edge_tts
import asyncio

ROOT = Path(__file__).resolve().parent
RENDER = ROOT / "render"
TOUR = ROOT / "tour"
BEATS = ROOT / "beats"
AUDIO = ROOT / "beats_audio"
BEATS.mkdir(exist_ok=True)
AUDIO.mkdir(exist_ok=True)
OUT = ROOT.parent.parent / "Dream_Job_Walkthrough_and_Architecture_v2.mp4"

VOICE = "en-US-BrianNeural"
RATE = "+12%"
FPS = 30
W, H = 1920, 1080

# --- timeline ---------------------------------------------------------------
tl = json.loads((TOUR / "timeline.json").read_text())
clip = {}
cur = None
for m in tl["marks"]:
    if m["name"].endswith(":start"):
        cur = m["t"]
    elif m["name"].endswith(":end") and cur is not None:
        name = m["name"][:-4]
        clip[name] = (max(0.0, cur + 0.25), m["t"])
        cur = None

# --- beat plan --------------------------------------------------------------
# ("slide", n, narration)  or  ("demo", clip_name, narration)
PLAN = [
    ("slide", 1, "Welcome to Dream Job, a working, end-to-end platform for an AI-assisted job search. "
                 "This is version two: every screen, diagram and count was taken from the running system."),
    ("slide", 2, "The video follows the deck. Part one walks the application screen by screen, from the "
                 "profile to the follow-up. Part two explains the architecture, and part three lists every "
                 "external source the platform can read."),
    ("slide", 3, "Version two is current. Two hundred and twenty-seven Python modules and one hundred and "
                 "forty JavaScript modules, over four hundred API routes, eighty-eight tables, twenty-nine "
                 "source adapters and two thousand and fifty-five passing tests. Screens added since the first "
                 "edition are listed on the right."),
    ("slide", 4, "In one picture: profile, plan, discover, apply and follow up. Each phase owns a colour "
                 "that follows it through every screen."),
    ("slide", 5, "Part one: a functional walkthrough, in the order a job seeker meets each screen."),
    ("slide", 6, "It starts with three steps and nothing else. Set up your profile, let Dream Job search "
                 "and rank the market, then choose who to write to."),
    ("demo", "home", "On today's data, one thousand and eighty-four opportunities were found, twenty-one of "
                     "them speculative, and nine hundred and thirty-nine are ranked. Nothing is ever sent on "
                     "its own."),
    ("slide", 7, "Where I am is the whole process on one screen: what is done, what is running, what is "
                 "blocked, and what the next useful move is."),
    ("demo", "overview", "Every stage links to the screen that advances it, and while a campaign runs its "
                         "token budget and cost refresh every few seconds."),
    ("slide", 8, "Everything begins on the profile. A LinkedIn export and a CV are merged into one profile, "
                 "and nothing generated later can say anything that is not grounded here."),
    ("demo", "profile", "This account holds twenty-two saved versions. Every save is a new version, and each "
                        "campaign records which version it used."),
    ("slide", 9, "The merge is honest about what it could not decide. Every disagreement is shown with both "
                 "values side by side and stays open until the job seeker chooses."),
    ("demo", "profile-conflicts", "Here, the LinkedIn export and the CV disagree about a role's dates. The "
                                  "screen surfaces it rather than silently picking one."),
    ("slide", 10, "The rest of the workspace is one click away: the structured sections, the normalised skill "
                  "taxonomy, evidence kept with its provenance, personas, and the privacy flags."),
    ("demo", "profile-skills", "Skills are normalised to a canonical taxonomy, and fields marked do not "
                               "disclose are stripped before anything reaches the model."),
    ("demo", "profile-evidence", "Evidence keeps each statement with its provenance, so anything later written "
                                 "into a CV can be traced to a source."),
    ("demo", "profile-personas", "Personas let one profile serve more than one kind of application."),
    ("demo", "profile-privacy", "Privacy marks the fields that must never be disclosed, which is also where "
                                "online enrichment is switched off."),
    ("slide", 11, "The composite profile is the synthesis the model actually reads: what the seeker supplied "
                  "plus what could be verified online, every statement carrying its source."),
    ("demo", "composite", "Ten blocks, from narrative and career trajectory to constraints and inferred "
                          "preferences. Each statement can be removed, and each carries a confidence."),
    ("slide", 12, "Online findings are queued for confirm or reject, and identity matching folds name variants, "
                  "employers, locations and photos before anything merges."),
    ("demo", "composite-findings", "A rejection is permanent, and enrichment can be switched off entirely so "
                                   "the profile is built from documents alone."),
    ("demo", "composite-enrichment", "Enrichment has its own settings: excluded domains, an off switch, and a "
                                     "confirmation before it is disabled."),
    ("slide", 13, "The dream job is described in your own words and language. The model reads it for nuance, "
                  "and deal-breakers become directive defaults."),
    ("demo", "dream-job", "The prose is not used for matching; the structured model is. And nothing acts on "
                          "that model until the seeker confirms it."),
    ("slide", 14, "Directives bound the search. They are structured controls, not free text, so they translate "
                  "reliably into each source's own query language."),
    ("demo", "directives", "Five groups, from job content to compensation, with discretion mode to exclude a "
                           "current employer, and a live estimate of sources and pages."),
    ("slide", 15, "A campaign is one run of the pipeline under one set of directives. Every stage stores what "
                  "it produced and can be re-run on its own."),
    ("demo", "campaigns", "The ledger shows status, stage, tokens used and cost for every run."),
    ("slide", 16, "Inside a campaign, the plan is reviewed and priced before launch: sources, queries, expected "
                  "volume and duration. The planner checks the knowledge base first and collects only what is "
                  "missing or stale."),
    ("demo", "campaign-plan", "Every source is listed with its native query and cost, with skip and reuse "
                              "controls."),
    ("demo", "campaign-live", "The live dashboard shows per-adapter progress, errors, tokens and cost. Pause, "
                              "resume or cancel at any point, and a crash loses at most the page in flight."),
    ("demo", "campaign-rerun", "Any single stage can be re-run without repeating the rest, which is how a broken "
                               "source is recovered."),
    ("slide", 17, "The browser session is attach-only. Dream Job attaches to a browser you already signed in "
                  "to and never holds a third-party password."),
    ("demo", "browser", "It reports whether a browser is attached and which sites are signed in, with human "
                        "pacing and an immediate stop on a challenge."),
    ("slide", 18, "Discover begins with the ranked list: every advertised vacancy and every speculative "
                  "opening, with the score advisory and the decision left to the seeker."),
    ("demo", "opportunities", "Filter by kind, arrangement, seniority, country or keyword; reorder by hand; and "
                              "reject with a reason, which feeds back into the weights."),
    ("slide", 19, "Why a role ranks where it does turns the score into an argument: seven sub-scores, the "
                  "dream-job criteria, a compensation estimate with its sources, and the route to a person."),
    ("demo", "opportunity-detail", "The dream-job fit shows met, partly met and violated criteria, not a single "
                                   "number, and a speculative opening explains why the system thinks it exists."),
    ("slide", 20, "Companies are the shared knowledge base: profiles, five years of financial analysis, "
                  "competitors and hiring signals, searchable without picking a campaign."),
    ("demo", "companies", "Today the inventory holds five thousand nine hundred and forty-one companies and "
                          "fifty-seven thousand vacancies, reused between searches while fresh."),
    ("slide", 21, "Inside a company, filed figures become two advisory scores that cite their inputs, and market "
                  "timing, peers and values are separate readings."),
    ("demo", "company-financials", "Five filed years reduce to ability to pay and investment capacity, and a "
                                   "profile built on secondary signals can never score above fifty-five."),
    ("demo", "company-values", "Values and culture shows what the company says about itself and where that "
                               "contradicts what the seeker asked for."),
    ("demo", "company-market", "Market and timing brings hiring signals, profile freshness, and peer companies "
                               "you can adopt into the analysis."),
    ("slide", 22, "Dream-job intelligence asks how far the market is from the job you described. Gap analysis "
                  "and stepping stones work without the language model."),
    ("demo", "intel-gaps", "Each gap says what closes it and where it was decisive on the ranked list. The "
                           "reading is stored, and recompute is explicit."),
    ("slide", 23, "When nothing clears the dream-job threshold, the system proposes routes that lead there, "
                  "shows how much of the market clears the bar, and drafts LinkedIn suggestions you copy by hand."),
    ("demo", "intel-stepping", "Stepping stones show two or three sequences of roles, each step tagged as "
                               "reachable now, next move, or the dream job itself."),
    ("demo", "intel-linkedin", "The LinkedIn advice is text you copy. Dream Job never signs in to LinkedIn and "
                               "never edits a profile."),
    ("demo", "intel-fit", "Fit across the market shows the distribution of dream-job fit and how much of it "
                          "clears the threshold you set."),
    ("demo", "intel-values", "Values conflicts flags companies whose own material contradicts what you said you "
                             "wanted, with a hard requirement called out."),
    ("slide", 24, "Apply starts with the people: hiring contacts found by walking a ladder of sources and "
                  "validated rather than guessed."),
    ("demo", "contacts", "A company marked unreachable is a real finding, and an objection blocks an address "
                         "permanently for everyone on the installation."),
    ("slide", 25, "Contact discovery scales: browse every contact, find missing addresses, walk warm "
                  "introduction routes, import your network, and honour objections and retention."),
    ("demo", "contacts-browse", "Browse all contacts with validation, source method and an unverified filter."),
    ("demo", "contacts-intros", "Introduction routes rank the warm paths into a target company by strength and "
                                "relevance."),
    ("demo", "contacts-network", "Your own network can be imported, so warm paths are found from people you "
                                 "already know."),
    ("demo", "contacts-retention", "Objections and retention honour a do-not-contact request, and sweep "
                                   "browser-collected data on a schedule."),
    ("slide", 26, "Then the applications themselves. The Apply browser shows the generated email, CV, briefing "
                  "and motivation, all for the seeker to read, edit and approve."),
    ("demo", "apply-email", "A consistency check catches invented employers, dates and titles, and a leak scan "
                            "keeps private documents out of the attachment."),
    ("slide", 27, "The four artefacts are generated together. The briefing and motivation never leave the "
                  "machine, and the checks gate the send."),
    ("demo", "apply-cv", "The generated CV is grounded in the profile. Nothing appears that cannot be traced "
                         "back to it."),
    ("demo", "apply-checks", "Approving authorises dispatch but sends nothing. Messages go from the seeker's "
                             "own mailbox, and the seeker is the last line of defence."),
    ("demo", "apply-briefing", "The briefing gives the seeker the context for the conversation, and is never "
                               "attached to the email."),
    ("demo", "apply-motivation", "The motivation is the longer statement, also for the seeker only."),
    ("slide", 28, "Networking adds warm introductions, an event radar with calendar export, and a "
                  "self-contained campaign package for a coach."),
    ("demo", "networking-events", "Events come with a match meter, calendar links, and an ics download."),
    ("demo", "networking-export", "The export is a PDF bundle plus machine-readable JSON, redacted the same way "
                                  "as the model path."),
    ("slide", 29, "Follow-up begins on the pipeline board: every authorised application and its stage, from sent "
                  "to reply, interview and outcome."),
    ("demo", "pipeline", "Open a card for the classified reply, the drafted answer and the stage dates. "
                         "Approving a draft still sends nothing."),
    ("slide", 30, "Responses are every answer, however it arrived: detected from Gmail or recorded by hand for "
                  "a call, a portal or a LinkedIn message."),
    ("demo", "responses", "Recording rejections matters as much as good news, and a response moves the card on "
                          "the board automatically."),
    ("slide", 31, "What works computes which kinds of job and company actually answer, always with the sample "
                  "size behind the rate."),
    ("demo", "insights", "Advice needs roughly six resolved applications before it speaks, and applying a "
                         "proposal creates a new directive version rather than overwriting."),
    ("slide", 32, "Monitoring is the operational surface: campaign health, token spend, extraction rates and "
                  "the audit trail, plus scheduled re-checks of watched companies."),
    ("demo", "monitoring", "Extraction-rate monitoring surfaces adapter breakage, and what changed on a watched "
                           "company becomes one weekly digest."),
    ("slide", 33, "Mail setup offers two ways to send from the seeker's own identity, Gmail or a Resend relay, "
                  "behind one dispatcher that holds every guard rail."),
    ("demo", "mail", "Sending rules, windows and a daily cap protect sender reputation; the dispatch log records "
                     "everything sent, and follow-ups are drafted, then approved."),
    ("slide", 34, "Administration is the installation-level control: the source catalogue and its terms "
                  "acknowledgements, server logs, model routing and data rights."),
    ("demo", "admin-sources", "Sources that require acknowledgement stay disabled until an administrator "
                              "confirms their terms."),
    ("demo", "admin-models", "Models chooses the provider, per-task routing and budget, and can point "
                             "privacy-sensitive work at a local endpoint."),
    ("slide", 35, "The other eight tabs cover activity, continuous collection, model routing and spend, audit, "
                  "logs, users, and the data rights tools."),
    ("demo", "admin-data", "Two actions cannot be undone: the redaction sweep and erasing a job seeker, which "
                           "leaves the shared market data standing."),
    ("slide", 36, "Every screen explains itself with inline tips, a per-screen help drawer, and first-run "
                  "guidance that works before any data exists."),
    ("demo", "help", "Press question mark anywhere to open the drawer for the screen you are on."),
    ("slide", 37, "Part two: the technical architecture."),
    ("slide", 38, "A React single-page application over a FastAPI API, a pipeline of stages, and five services "
                  "over a repository layer and SQLite."),
    ("slide", 39, "The architecture funnels five concerns through exactly one module each: every SQL statement, "
                  "every outbound request, every model call, every source, and every long task."),
    ("slide", 40, "One campaign, end to end: plan, reuse, collect, analyse and rank, then apply and learn. "
                  "Replies stay private to one seeker."),
    ("slide", 41, "Every stage persists its own artefacts and can be re-run on its own. The user-facing "
                  "pipeline is the five phases."),
    ("slide", 42, "The adapter contract is plan, fetch, parse and normalise. A new source is one subclass and "
                  "one decorator."),
    ("slide", 43, "Every model call takes one path: redact, route and fence; check the budget; validate the "
                  "response; then debit and audit it."),
    ("slide", 44, "No other module opens an HTTP connection. Cache, robots, pacing, capture and content hashing "
                  "all live in the egress client."),
    ("slide", 45, "One database, three scopes: forty-nine private tables, thirty-eight shared, and the migration "
                  "ledger. No shared row links back to a job seeker."),
    ("slide", 46, "Every long task is resumable: jobs run on their own threads with checkpoints, so a paused job "
                  "resumes where it stopped."),
    ("slide", 47, "Measured, the pipeline is the centre of gravity, with the interface and the tests as the next "
                  "largest bodies of work."),
    ("slide", 48, "Route-level code splitting keeps the first paint small: the shared entry is three hundred and "
                  "twelve kilobytes, and the heaviest screen loads only when opened."),
    ("slide", 49, "One hundred and fifty-six of one hundred and fifty-seven requirements are cited in the "
                  "implementation. The one that is not is a document rather than code."),
    ("slide", 50, "Every rank is a weighted mean over seven named sub-scores, with weights that are visible and "
                  "learned from outcomes. The score orders a list; it never decides."),
    ("slide", 51, "Learning refuses to overstate: outcome rates carry a Wilson interval and a sample size, and "
                  "the system will not advise below six resolved applications."),
    ("slide", 52, "Filed accounts reduce to two advisory scores, weighted and coverage-shaded, never claiming "
                  "more than the evidence supports."),
    ("slide", 53, "Part three: the data sources."),
    ("slide", 54, "Twenty-nine adapters register themselves at import time, and four require an explicit terms "
                  "acknowledgement before they can be enabled."),
    ("slide", 55, "Nine applicant tracking systems give structured job feeds, so collection costs no tokens."),
    ("slide", 56, "Nine job boards and public employment services cover Belgium, the Netherlands, the EU and "
                  "globally, with terms status deciding what is enabled by default."),
    ("slide", 57, "Registries, directories, news, events, websites and a compensation benchmark supply the "
                  "non-vacancy signals."),
    ("slide", 58, "The planner selects from the catalogue using the coverage each adapter declares, and every "
                  "source is reached through the egress client."),
    ("slide", 59, "In five sentences: profile in; plan and collect; analyse and rank; apply with consent at "
                  "every gate; then follow up and learn."),
    ("slide", 60, "That is Dream Job, version two. Every screen, diagram and count in this video was taken from "
                  "the running system."),
]


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def tts(text, path):
    c = edge_tts.Communicate(text, VOICE, rate=RATE)
    await c.save(str(path))


def dur(path):
    return float(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path)]).strip())


async def prepare_audio():
    infos = []
    for i, beat in enumerate(PLAN):
        p = AUDIO / f"b{i:03d}.mp3"
        if not p.exists() or p.stat().st_size == 0:
            await tts(beat[2], p)
        infos.append(round(dur(p), 3))
    return infos


def slide_clip(n, nar, out, duration):
    png = RENDER / f"slide-{n:02d}.png"
    if not png.exists():
        raise FileNotFoundError(png)
    run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(png), "-i", str(nar),
        "-filter_complex",
        f"[0:v]scale={W}:{H},fps={FPS},format=yuv420p[v];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={duration},atrim=0:{duration}[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{duration}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-g", "60",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p", str(out)])


def demo_clip(name, nar, out, duration):
    s, e = clip[name]
    run([
        "ffmpeg", "-y", "-ss", f"{s}", "-to", f"{e}", "-i", str(TOUR_VIDEO), "-i", str(nar),
        "-filter_complex",
        f"[0:v]scale={W}:{H},fps={FPS},setpts=PTS-STARTPTS[v0];"
        f"[v0]tpad=stop_mode=clone:stop_duration={duration},trim=0:{duration},format=yuv420p[v];"
        f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={duration},atrim=0:{duration}[a]",
        "-map", "[v]", "-map", "[a]", "-t", f"{duration}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-g", "60",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-pix_fmt", "yuv420p", str(out)])


async def main():
    global TOUR_VIDEO
    TOUR_VIDEO = next(TOUR.glob("*.webm"))
    print("narration...")
    durs = await prepare_audio()
    print("building beats...")
    listfile = BEATS / "list.txt"
    with listfile.open("w") as lf:
        for i, beat in enumerate(PLAN):
            kind = beat[0]
            nd = durs[i]
            out = BEATS / f"beat{i:03d}.mp4"
            if kind == "slide":
                d = nd + 1.4
                slide_clip(beat[1], AUDIO / f"b{i:03d}.mp3", out, round(d, 3))
                label = f"slide {beat[1]}"
            else:
                d = max(nd + 0.7, 5.2)
                demo_clip(beat[1], AUDIO / f"b{i:03d}.mp3", out, round(d, 3))
                label = f"demo {beat[1]}"
            print(f"  {i+1:02d}/{len(PLAN)} {label:28} {d:5.2f}s")
            lf.write(f"file '{out}'\n")
    print("concatenating...")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
         "-c", "copy", "-movflags", "+faststart", str(OUT)])
    print("wrote", OUT, "%.1f s" % dur(OUT))


if __name__ == "__main__":
    asyncio.run(main())
