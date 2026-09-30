"""Build Dream_Job_Walkthrough_and_Architecture_v2.pptx.

Matches the visual language of v1 (Helvetica Neue, the five phase hues, the
left accent bar, eyebrow/title/intro/panel/footer furniture) but every number,
diagram, screenshot and explanation is refreshed from the current codebase and
covers the full set of screens, including the ones v1 omitted.

Run:  python build_pptx.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
SHOTS = ROOT / "shots"
FIGS = REPO / "docs" / "figures" / "out"
OUT = REPO / "docs" / "Dream_Job_Walkthrough_and_Architecture_v2.pptx"

F = json.loads((ROOT / "facts_v2.json").read_text())
SOURCES = json.loads((ROOT / "sources.json").read_text())

# --- palette ----------------------------------------------------------------
INK = RGBColor(0x1A, 0x1A, 0x22)
INK2 = RGBColor(0x4A, 0x4A, 0x58)
INK3 = RGBColor(0x74, 0x74, 0x8A)
LINE = RGBColor(0xD8, 0xD6, 0xE2)
LINE_SOFT = RGBColor(0xEC, 0xEA, 0xF3)
SURFACE = RGBColor(0xFF, 0xFF, 0xFF)
SURFACE2 = RGBColor(0xFA, 0xF9, 0xFD)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
OK = RGBColor(0x1F, 0x8A, 0x5A)
WARN = RGBColor(0x9F, 0x60, 0x11)

PHASE = {
    1: RGBColor(0x6D, 0x54, 0xD8),
    2: RGBColor(0x2D, 0x6B, 0xC8),
    3: RGBColor(0x0B, 0x7B, 0x73),
    4: RGBColor(0x9F, 0x60, 0x11),
    5: RGBColor(0xBC, 0x3D, 0x66),
    0: RGBColor(0x5F, 0x66, 0x72),
}
PHASE_SOFT = {
    1: RGBColor(0xF0, 0xEC, 0xFD),
    2: RGBColor(0xE9, 0xF1, 0xFD),
    3: RGBColor(0xE2, 0xF4, 0xF2),
    4: RGBColor(0xFC, 0xF1, 0xDE),
    5: RGBColor(0xFD, 0xEA, 0xF0),
    0: RGBColor(0xF1, 0xF2, 0xF4),
}
FONT = "Helvetica Neue"

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]


# --- primitives -------------------------------------------------------------
def textbox(sl, x, y, w, h, anchor=MSO_ANCHOR.TOP):
    tb = sl.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    return tf


def para(tf, text, size, color=INK, bold=False, space_after=6, first=False,
         bullet=False, align=PP_ALIGN.LEFT, italic=False, space_before=0):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_after = Pt(space_after)
    p.space_before = Pt(space_before)
    p.line_spacing = 1.12
    r = p.add_run()
    r.text = ("•  " + text) if bullet else text
    r.font.name = FONT
    r.font.size = Pt(size)
    r.font.bold = bold
    r.font.italic = italic
    r.font.color.rgb = color
    return p


def rect(sl, x, y, w, h, fill=None, line=None, shape=MSO_SHAPE.RECTANGLE, radius=None):
    sh = sl.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    if fill is None:
        sh.fill.background()
    else:
        sh.fill.solid()
        sh.fill.fore_color.rgb = fill
    if line is None:
        sh.line.fill.background()
    else:
        sh.line.color.rgb = line
        sh.line.width = Pt(1)
    sh.shadow.inherit = False
    return sh


def accent(sl, phase):
    rect(sl, 0, 0, 0.14, 7.5, fill=PHASE[phase])


def eyebrow(sl, text, phase):
    tf = textbox(sl, 0.55, 0.34, 11.5, 0.32)
    para(tf, text, 10.5, PHASE[phase], bold=True, first=True, space_after=0)


def title(sl, text):
    tf = textbox(sl, 0.55, 0.60, 11.9, 0.72)
    para(tf, text, 27, INK, bold=True, first=True, space_after=0)


def lead(sl, text, y=1.28, w=12.2, size=12.5, color=INK2):
    tf = textbox(sl, 0.55, y, w, 0.6)
    para(tf, text, size, color, first=True, space_after=0)


def bullets(sl, items, x, y, w, h, size=10.5, color=INK2, lead_text=None, lead_size=11.5):
    tf = textbox(sl, x, y, w, h)
    first = True
    if lead_text:
        para(tf, lead_text, lead_size, INK, first=True, space_after=10)
        first = False
    for it in items:
        para(tf, it, size, color, first=first, bullet=True, space_after=8)
        first = False
    return tf


def caption(sl, text, x, y, w):
    tf = textbox(sl, x, y, w, 0.3)
    para(tf, text, 8.5, INK3, first=True, space_after=0)


def footer(sl, n):
    tf = textbox(sl, 0.55, 7.06, 9.0, 0.3)
    para(tf, "Dream Job  ·  Functional walkthrough and technical architecture  ·  v2",
         8.5, INK3, first=True, space_after=0)
    tf2 = textbox(sl, 12.20, 7.06, 0.6, 0.3)
    para(tf2, str(n), 8.5, INK3, first=True, space_after=0, align=PP_ALIGN.RIGHT)


def picture(sl, path, x, y, w, h):
    """Insert an image contained (aspect preserved) and centred inside the box."""
    path = Path(path)
    iw, ih = Image.open(path).size
    box_ar = w / h
    img_ar = iw / ih
    if img_ar > box_ar:
        pw, ph = w, w / img_ar
    else:
        ph, pw = h, h * img_ar
    px, py = x + (w - pw) / 2, y + (h - ph) / 2
    return sl.shapes.add_picture(str(path), Inches(px), Inches(py), Inches(pw), Inches(ph))


def panel(sl, x, y, w, h, fill=SURFACE2, line=LINE, radius=0.06):
    sh = rect(sl, x, y, w, h, fill=fill, line=line,
              shape=MSO_SHAPE.ROUNDED_RECTANGLE)
    try:
        sh.adjustments[0] = radius
    except Exception:
        pass
    return sh


def table(sl, x, y, w, h, columns, rows, widths=None, header_phase=0,
          font_size=9.5, header_size=9.5, row_height=0.34):
    shape = sl.shapes.add_table(len(rows) + 1, len(columns), Inches(x), Inches(y), Inches(w), Inches(h))
    tbl = shape.table
    tbl.first_row = True
    tbl.horz_banding = True
    if widths:
        total = sum(widths)
        for i, cw in enumerate(widths):
            tbl.columns[i].width = Emu(int(Inches(w) * cw / total))
    for j, col in enumerate(columns):
        c = tbl.cell(0, j)
        c.text = col
        c.fill.solid()
        c.fill.fore_color.rgb = PHASE[header_phase]
        p = c.text_frame.paragraphs[0]
        p.runs[0].font.size = Pt(header_size)
        p.runs[0].font.bold = True
        p.runs[0].font.name = FONT
        p.runs[0].font.color.rgb = WHITE
        c.vertical_anchor = MSO_ANCHOR.MIDDLE
        c.margin_left = Inches(0.08); c.margin_right = Inches(0.06)
        c.margin_top = Inches(0.02); c.margin_bottom = Inches(0.02)
    for i, row in enumerate(rows, 1):
        tbl.rows[i].height = Inches(row_height)
        for j, val in enumerate(row):
            c = tbl.cell(i, j)
            c.text = str(val)
            c.vertical_anchor = MSO_ANCHOR.MIDDLE
            c.margin_left = Inches(0.08); c.margin_right = Inches(0.06)
            c.margin_top = Inches(0.01); c.margin_bottom = Inches(0.01)
            c.fill.solid()
            c.fill.fore_color.rgb = SURFACE if i % 2 else SURFACE2
            p = c.text_frame.paragraphs[0]
            p.runs[0].font.size = Pt(font_size)
            p.runs[0].font.name = FONT
            p.runs[0].font.color.rgb = INK if j == 0 else INK2
            if j == 0:
                p.runs[0].font.bold = True
    return tbl


def img(name, sub="shots"):
    base = SHOTS if sub == "shots" else FIGS
    p = base / f"{name}.png"
    if not p.exists():
        raise FileNotFoundError(p)
    return p


N = 0


def new_slide(phase=0):
    global N
    N += 1
    sl = prs.slides.add_slide(BLANK)
    rect(sl, 0, 0, 13.333, 7.5, fill=SURFACE)
    if phase != 0 or True:
        accent(sl, phase)
    return sl


def screen_slide(eyebrow_text, title_text, lead_text, bullet_items, shot,
                 phase=1, caption_text=None, lead_size=12.0, bullet_size=10.3):
    sl = new_slide(phase)
    eyebrow(sl, eyebrow_text, phase)
    title(sl, title_text)
    lead(sl, lead_text, size=lead_size)
    panel(sl, 0.55, 1.5, 3.55, 4.82, fill=PHASE_SOFT[phase], line=LINE_SOFT)
    bullets(sl, bullet_items, 0.79, 1.74, 3.07, 4.4, size=bullet_size, color=INK2)
    panel(sl, 4.84, 1.55, 7.71, 4.82, fill=SURFACE, line=LINE)
    picture(sl, img(shot), 4.79, 1.50, 7.71, 4.82)
    caption(sl, caption_text or f"{shot}  ·  captured from the running application",
            4.50, 6.50, 8.30)
    footer(sl, N)
    return sl


def figure_slide(eyebrow_text, title_text, lead_text, figure, phase=2,
                 caption_text=None, notes=None):
    sl = new_slide(phase)
    eyebrow(sl, eyebrow_text, phase)
    title(sl, title_text)
    if lead_text:
        lead(sl, lead_text)
    top = 1.9 if lead_text else 1.55
    if notes:
        panel(sl, 0.55, top, 3.55, 4.6, fill=PHASE_SOFT[phase], line=LINE_SOFT)
        bullets(sl, notes, 0.79, top + 0.24, 3.07, 4.2, size=10.2)
        picture(sl, img(figure, "figs"), 4.84, top - 0.05, 7.71, 4.72)
    else:
        picture(sl, img(figure, "figs"), 0.55, top, 12.2, 4.6)
    if caption_text:
        caption(sl, caption_text, 0.55, 6.55, 12.2)
    footer(sl, N)
    return sl


def bullets_slide(eyebrow_text, title_text, lead_text, items, phase=0,
                  columns=1, size=12.5, notes=None):
    sl = new_slide(phase)
    eyebrow(sl, eyebrow_text, phase)
    title(sl, title_text)
    if lead_text:
        lead(sl, lead_text, y=1.3, size=13)
    y0 = 2.05 if lead_text else 1.55
    if columns == 2:
        half = len(items) // 2 + len(items) % 2
        bullets(sl, items[:half], 0.55, y0, 5.95, 4.6, size=size, color=INK2)
        bullets(sl, items[half:], 6.85, y0, 5.95, 4.6, size=size, color=INK2)
    else:
        bullets(sl, items, 0.55, y0, 12.2, 4.7, size=size, color=INK2)
    footer(sl, N)
    return sl


def table_slide(eyebrow_text, title_text, lead_text, columns, rows, widths,
                phase=3, font_size=9.0, header_size=9.5, row_height=0.33,
                notes=None):
    sl = new_slide(phase)
    eyebrow(sl, eyebrow_text, phase)
    title(sl, title_text)
    if lead_text:
        lead(sl, lead_text, y=1.28, size=12)
    y = 1.95 if lead_text else 1.6
    h = 5.05 if lead_text else 5.4
    if notes:
        bullets(sl, notes, 0.55, y, 3.05, h, size=9.8, color=INK2)
        table(sl, 3.9, y, 8.88, h, columns, rows, widths, header_phase=phase,
              font_size=font_size, header_size=header_size, row_height=row_height)
    else:
        table(sl, 0.55, y, 12.23, h, columns, rows, widths, header_phase=phase,
              font_size=font_size, header_size=header_size, row_height=row_height)
    footer(sl, N)
    return sl


def montage_slide(eyebrow_text, title_text, lead_text, items, phase=1,
                  cols=2, caption_prefix=""):
    """items: list of (shot, caption)."""
    sl = new_slide(phase)
    eyebrow(sl, eyebrow_text, phase)
    title(sl, title_text)
    if lead_text:
        lead(sl, lead_text, size=12.0)
    x0, y0 = 0.55, 1.95
    gap = 0.25
    total_w = 12.23
    cw = (total_w - gap * (cols - 1)) / cols
    n = max(1, len(items))
    rows = (n + cols - 1) // cols
    avail = 6.72 - y0 - (rows - 1) * 0.42
    ch = min(3.2, avail / rows)
    for i, (shot, cap) in enumerate(items):
        r, c = divmod(i, cols)
        x = x0 + c * (cw + gap)
        y = y0 + r * (ch + 0.42)
        panel(sl, x, y, cw, ch, fill=SURFACE, line=LINE)
        picture(sl, img(shot), x + 0.06, y + 0.06, cw - 0.12, ch - 0.12)
        caption(sl, cap, x, y + ch + 0.04, cw)
    footer(sl, N)
    return sl


# ===========================================================================
# COVER
# ===========================================================================
sl = new_slide(0)
rect(sl, 0, 0, 13.333, 0.16, fill=PHASE[1])
rect(sl, 2.667, 0, 2.666, 0.16, fill=PHASE[2])
rect(sl, 5.333, 0, 2.666, 0.16, fill=PHASE[3])
rect(sl, 8.0, 0, 2.666, 0.16, fill=PHASE[4])
rect(sl, 10.667, 0, 2.666, 0.16, fill=PHASE[5])
logo = REPO / "frontend" / "public" / "logo-lockup.png"
if logo.exists():
    sl.shapes.add_picture(str(logo), Inches(0.9), Inches(1.0), height=Inches(0.5))
tf = textbox(sl, 0.9, 2.0, 11.5, 1.7)
para(tf, "Dream Job", 46, INK, bold=True, first=True, space_after=2)
para(tf, "Functional walkthrough and technical architecture  ·  v2", 24, PHASE[1], bold=True, space_after=0)
tf = textbox(sl, 0.9, 4.0, 11.0, 1.2)
para(tf, "An AI-assisted job discovery and application platform: profile in, ranked "
         "and explained opportunities out, every message reviewed and sent by the job "
         "seeker. Version 2 refreshes every screenshot, diagram and count, and covers "
         "the screens added since the first edition.", 13.5, INK2, first=True, space_after=0)
rect(sl, 0.9, 5.5, 1.4, 0.04, fill=PHASE[1])
tf = textbox(sl, 0.9, 5.7, 11.0, 0.9)
para(tf, "Prepared for Stephane van der Aa  |  Live application walkthrough, system "
         "architecture and the full source catalogue  ·  September 2026", 12, INK2, bold=True, first=True)
footer(sl, N)

# ===========================================================================
# CONTENTS
# ===========================================================================
sl = new_slide(0)
eyebrow(sl, "OVERVIEW", 0)
title(sl, "What this deck contains")
lead(sl, "A working tour of the platform, the architecture behind it, and the complete "
         "list of external sources it can read.", y=1.3, size=12.5)
bullets(sl, [
    "Part 1 · A functional walkthrough: a screenshot of every working screen — including "
    "Browser session, Apply browser, Networking and Mail setup, which the first edition "
    "did not cover — with what each screen is for and how the job seeker uses it.",
    "Part 2 · Technical architecture: the layered system, the five chokepoints, the "
    "campaign pipeline end to end, the adapter contract, the path of one LLM call and one "
    "outbound request, and the data model.",
    "Part 3 · Data sources: every registered adapter — 29 today — grouped by type, with "
    "coverage, access method, terms status and whether it needs the language model.",
    "Throughout: diagrams and figures are generated from the code, and every count in "
    "this deck was measured from the working tree.",
    "The principle throughout: nothing is sent or applied on its own; the job seeker is "
    "the last line of defence and the only sender.",
], 0.55, 2.0, 12.2, 4.9, size=12.5)
footer(sl, N)

# ===========================================================================
# WHAT'S NEW IN V2
# ===========================================================================
sl = new_slide(2)
eyebrow(sl, "OVERVIEW · WHAT CHANGED", 2)
title(sl, "What is new in version 2")
lead(sl, "Every figure below was re-measured from the current codebase; screens added "
         "since the first edition are listed on the right.", y=1.28, size=12)
table(sl, 0.55, 1.95, 6.0, 4.4,
      ["Metric", "v1", "v2 (measured)"],
      [["Python modules", "172", F"{F['backend_files']}  ({F['backend_lines']:,} lines)"],
       ["JS/JSX modules", "113", f"{F['frontend_files']}  ({F['frontend_lines']:,} lines)"],
       ["API routes", "301", f"{F['api_routes']} over {F['api_routers']} routers"],
       ["Database tables", "71", f"{F['db_tables']}  ({F['db_private']} private, {F['db_shared']} shared)"],
       ["Indexes", "144", f"{F['db_indexes']}"],
       ["Migrations", "34", f"{F['migrations']}"],
       ["Tests", "1,725", f"{F['tests_passing']:,} passing of {F['tests_collected']:,}  ({F['test_files']} files)"],
       ["Adapters", "29", f"{F['adapters']}  ({F['adapters_ack']} need acknowledgement)"],
       ["Prompt templates", "24", f"{F['prompt_templates']}"]],
      widths=[2.0, 1.1, 2.9], header_phase=2, font_size=10, header_size=10, row_height=0.43)
bullets(sl, [
    "New screens: Browser session, Apply browser, Networking and export, Mail setup, and "
    "the per-screen Help drawer.",
    "New depth within screens: Profile tabs (Sections, Skills, Evidence, Personas, "
    "Privacy), Composite (Online findings, Enrichment), Campaign (Plan review, Live "
    "dashboard, Re-run a stage), Company (Profile, Financials, Market and timing, Values "
    "and culture).",
    "New depth continued: Contacts (Browse all, Introduction routes, My network, "
    "Objections and retention), Dream-job intelligence (Gaps, Stepping stones, Fit across "
    "the market, Values conflicts, LinkedIn profile), Administration (nine tabs).",
    "Refreshed diagrams: all 20 generated figures and every screenshot in this deck come "
    "from the current system.",
], 6.85, 1.95, 5.95, 4.6, size=9.8)
footer(sl, N)

# ===========================================================================
# PRODUCT IN ONE PICTURE
# ===========================================================================
figure_slide("OVERVIEW", "The product in one picture", None, "fdd-01-journey",
             phase=1, caption_text="The five phases of the journey. Each phase owns a "
             "colour that follows it through every screen of the application.",
             notes=["Profile — import the facts about you and describe the job you want.",
                    "Plan — bound the search with structured directives and review the plan.",
                    "Discover — collect, profile, analyse and rank the market.",
                    "Apply — write, check and dispatch applications you approve.",
                    "Follow up — read the outcomes and redirect the next search."])

# ===========================================================================
# PART 1 · DIVIDER
# ===========================================================================
sl = new_slide(1)
tf = textbox(sl, 0.9, 2.35, 11.0, 0.5)
para(tf, "PART 1", 14, PHASE[1], bold=True, first=True)
tf = textbox(sl, 0.9, 2.7, 11.3, 1.0)
para(tf, "A functional walkthrough", 34, INK, bold=True, first=True)
tf = textbox(sl, 0.9, 3.9, 9.8, 1.4)
para(tf, "Every screen, in the order a job seeker meets it. The screenshots are taken "
         "from the running application with real data.", 13.5, INK2, first=True)
footer(sl, N)

# ===========================================================================
# START HERE and WHERE I AM
# ===========================================================================
screen_slide("PART 1 · START HERE", "Start here", 
    "Three steps and nothing else: set up your profile, let Dream Job search and rank "
    "the market, then choose who to write to. Everything in between runs on its own.",
    ["The whole search runs in the background; the page can be closed.",
     "Live counts on today's data: 1,084 opportunities found, 21 speculative, 939 ranked, "
     "4 with a contact.",
     "Nothing is ever sent on its own — every message waits for approval.",
     "A speculative opening is labelled everywhere so an email never claims a vacancy exists."],
    "home", phase=1, caption_text="home  ·  three steps, one action each")

screen_slide("PART 1 · SYSTEM", "Where I am — the full journey map",
    "The whole process on one screen: what is done, what is running, what is blocked, "
    "and what the next useful move is. Every stage links to the screen that advances it.",
    ["A green mark is finished, a pulsing one is running, a dotted one is waiting on an "
     "earlier stage.",
     "Headline counts are links to the screens their numbers come from.",
     "While a campaign runs, its token budget and cost refresh every few seconds.",
     "A blocked stage says what it needs, so the next move is never a guess."],
    "overview", phase=2, caption_text="overview  ·  the journey map over the live campaign")

# ===========================================================================
# PROFILE
# ===========================================================================
screen_slide("PART 1 · PROFILE", "Your profile",
    "Everything the system does starts here. The profile is the only source of facts "
    "about the job seeker; nothing in a generated CV or email can say anything not "
    "grounded in this page.",
    ["Import the LinkedIn export and the CV; the two are merged, and conflicts are "
     "surfaced, not silently resolved (FR-102/103).",
     "This account holds 22 saved versions; every save is a new version and campaigns "
     "record which version they used.",
     "Five extra tabs sit one click away: Sections, Skills, Evidence, Personas and Privacy.",
     "Fields marked 'do not disclose' are stripped before anything reaches the AI provider "
     "(FR-106)."],
    "profile-documents", phase=1, caption_text="profile · Documents — the two uploads")

montage_slide("PART 1 · PROFILE", "Resolving the disagreements",
    "The merge is honest about what it could not decide: every conflict is shown with "
    "both values side by side and stays open until the job seeker chooses.",
    [("profile-conflicts", "profile · Conflicts — contested values, resolved by hand"),
     ("profile-skills", "profile · Skills — the normalised, canonical skill taxonomy"),
     ("profile-evidence", "profile · Evidence — statements kept with their provenance"),
     ("profile-privacy", "profile · Privacy — fields marked do not disclose")],
    phase=1, cols=2)

montage_slide("PART 1 · PROFILE", "The rest of the profile workspace",
    "Personas let one profile serve more than one kind of application; sections show the "
    "structured profile the model actually reads.",
    [("profile-sections", "profile · Sections — the structured profile"),
     ("profile-personas", "profile · Personas — one profile, more than one angle"),
     ("profile-dreamjob-tab", "profile · Dream job tab — a summary with a link to the editor"),
     ("profile", "profile · the default view, with version and conflict counters")],
    phase=1, cols=2)

screen_slide("PART 1 · PROFILE", "Composite profile",
    "A synthesis of what the job seeker supplied and what could be verified online. This "
    "is what the model actually reads when matching, scoring and writing.",
    ["Every statement carries its source and a confidence.",
     "Ten blocks from narrative and career trajectory to constraints and inferred "
     "preferences.",
     "Online findings are queued for confirm or reject; a rejection is permanent (FR-124).",
     "Enrichment can be switched off; the profile is then built from documents alone."],
    "composite-tab", phase=1, caption_text="composite · the traceable synthesis")

montage_slide("PART 1 · PROFILE", "Online findings and enrichment",
    "Identity matching folds name variants, employer overlap, cross-links, location, "
    "timeline and photo similarity; only confirmed findings merge (RK-02).",
    [("composite-findings", "composite · Online findings — confirm or reject, permanently"),
     ("composite-enrichment", "composite · Enrichment — switch it off, exclude domains"),
     ("profile-dreamjob-tab", "composite · the composite in context"),
     ("profile-conflicts", "the same discipline as profile conflicts")],
    phase=1, cols=2)

screen_slide("PART 1 · PROFILE", "Dream job",
    "Describe the job you actually want, in your own words and language. The model reads "
    "it for nuance — target roles, responsibilities, values and deal-breakers.",
    ["Deal-breakers filter more decisively than preferences; they become directive defaults.",
     "The structured model is used for matching; the prose is not.",
     "The screen flags when the statement and the model have drifted apart.",
     "Nothing acts on the model until the job seeker confirms it (FR-128)."],
    "dream-job", phase=1, caption_text="dream job · statement plus structured model")

# ===========================================================================
# PLAN
# ===========================================================================
screen_slide("PART 1 · PLAN", "Search directives",
    "Directives bound the search. They are structured controls rather than free text, so "
    "they translate reliably into each source's own query language.",
    ["Five groups: job content, company type, location, work arrangement and compensation.",
     "Values pre-filled from the composite profile are suggestions, not facts.",
     "Discretion mode excludes the current employer and its group entities from every "
     "search and message (FR-385).",
     "The estimate beside the form says how many sources and pages the settings imply."],
    "directives", phase=2, caption_text="directives · structured controls and a live estimate")

screen_slide("PART 1 · PLAN", "Campaigns",
    "A campaign is one run of the collection and analysis pipeline under one set of "
    "directives. Every stage stores what it produced and can be re-run on its own.",
    ["The plan is reviewed before launch: sources, queries, expected volume, duration and "
     "cost.",
     "The planner checks the shared knowledge base first and collects only what is missing "
     "or stale (FR-342/343).",
     "Pause, resume or cancel at any point; a crash loses at most the page in flight "
     "(NFR-401).",
     "The token budget caps AI spend and degrades gracefully near the limit."],
    "campaigns", phase=2, caption_text="campaigns · the ledger of runs")

montage_slide("PART 1 · PLAN", "Inside a campaign",
    "Every stage persists its own artefacts; the plan is a human gate before collection, "
    "and the dashboard is the live view of what each adapter is doing.",
    [("campaign-plan", "campaign · Plan review — sources, queries, cost, reuse"),
     ("campaign-live", "campaign · Live dashboard — per-adapter progress, tokens, cost"),
     ("campaign-rerun", "campaign · Re-run a stage — one stage, without the rest (NFR-603)"),
     ("campaign-detail", "campaign · the campaign header and state actions")],
    phase=2, cols=2)

screen_slide("PART 1 · PLAN", "Browser session",
    "The control surface for browser-assisted collection. Dream Job attaches to a browser "
    "you already signed in to; it never holds a third-party password (FR-201/202).",
    ["Connection panel reports whether a browser is attached and which sites are signed in.",
     "Launch instructions per operating system; the session is attach-only.",
     "Human pacing, a duration contract and immediate stop on a challenge (CR-401).",
     "Retention and purge keep browser-collected data on a short leash (NFR-303/344)."],
    "browser", phase=2, caption_text="browser · connection, pacing and terms acknowledgement")

# ===========================================================================
# DISCOVER
# ===========================================================================
screen_slide("PART 1 · DISCOVER", "Opportunities",
    "Every advertised vacancy and every speculative opening the system found, ranked. "
    "The score is advisory; the job seeker decides what to pursue.",
    ["Filter and sort by kind, arrangement, seniority, country, score floor or keyword.",
     "'Why this rank?' opens the seven sub-scores and the dream-job criteria behind them.",
     "Manual ordering overrides the computed ranking and survives recalculation (FR-284).",
     "Rejection reasons feed back into the weights, which is why a reason is required."],
    "opportunities", phase=3, caption_text="opportunities · the ranked list, filtered and sortable")

screen_slide("PART 1 · DISCOVER", "Why a role ranks where it does",
    "The detail screen turns the score into an argument: seven sub-scores, the dream-job "
    "criteria, the compensation estimate and its sources, and the route to a person.",
    ["Sub-scores: profile fit, dream-job fit, directive fit, company, compensation, "
     "plausibility, reachability.",
     "Dream-job fit shows met, partly met and violated criteria, not a single number.",
     "A speculative opening explains why the system thinks the role exists (FR-263).",
     "'Prepare an application for this role' is the bridge into Apply."],
    "opportunity-detail", phase=3, caption_text="opportunity detail · the full explanation")

screen_slide("PART 1 · DISCOVER", "Companies and the knowledge base",
    "The shared knowledge base: company profiles, five-year financial analyses, "
    "competitors and hiring signals — searchable without picking a campaign.",
    ["Company research is shared across job seekers and reused while fresh, so a second "
     "search costs nothing.",
     "Freshness is shown before the figures: a stale row describes the company as it was.",
     "Suppression and watch controls, plus an employer-kind verdict for who is hiring.",
     "The inventory today holds 5,941 companies and 57,293 vacancies."],
    "companies", phase=3, caption_text="companies · the shared inventory")

montage_slide("PART 1 · DISCOVER", "Inside a company",
    "Five years of filed figures become two advisory scores that cite their inputs; "
    "market timing, peers and values are separate tabs.",
    [("company-financials", "company · Financials — five-year analysis and two scores"),
     ("company-market", "company · Market and timing — signals and peers to adopt"),
     ("company-values", "company · Values and culture — what the company says about itself"),
     ("company-detail", "company · the header: trajectory, freshness, watch and suppress")],
    phase=3, cols=2)

screen_slide("PART 1 · DISCOVER", "Dream-job intelligence — the gaps",
    "How far the market and your profile are from the job you described, and what would "
    "close the distance. Gap analysis and paths work without the language model.",
    ["Gaps are computed against the structured dream-job model, with a threshold the job "
     "seeker controls.",
     "Each gap says what closes it and where it was decisive on the ranked list.",
     "The reading is stored; recompute is explicit, nothing recalculates behind your back.",
     "The numbers are deterministic; a model only sharpens the wording (CR-405)."],
    "intel-gaps", phase=3, caption_text="intelligence · Gaps and what closes them")

montage_slide("PART 1 · DISCOVER", "Stepping stones, fit and values",
    "When nothing clears the dream-job threshold, the system proposes two or three "
    "sequences of roles that lead there — and shows how much of the market clears it at all.",
    [("intel-stepping", "intelligence · Stepping stones — routes to the dream job"),
     ("intel-fit", "intelligence · Fit across the market — the distribution and threshold"),
     ("intel-values", "intelligence · Values conflicts — where a company contradicts you"),
     ("intel-linkedin", "intelligence · LinkedIn profile — suggestions you copy by hand")],
    phase=3, cols=2)

# ===========================================================================
# APPLY
# ===========================================================================
screen_slide("PART 1 · APPLY", "Hiring contacts",
    "The people behind the opportunities, found by walking a ladder of sources and "
    "validated rather than guessed.",
    ["A company showing 'unreachable' is a real finding: nothing survived the ladder.",
     "Contacts are shared unless collected by browser automation, then restricted "
     "(NFR-302/303).",
     "An objection blocks the address permanently for everyone on the installation.",
     "The knowledge base holds 3,959 contacts today."],
    "contacts-tab", phase=4, caption_text="contacts · the ranked ladder for a company")

montage_slide("PART 1 · APPLY", "Contact discovery at scale",
    "Browse every contact, find missing addresses, walk warm introduction routes, import "
    "your network and honour objections and retention.",
    [("contacts-browse", "contacts · Browse all — filters and the missing-email panel"),
     ("contacts-intros", "contacts · Introduction routes — warm paths into a company"),
     ("contacts-network", "contacts · My network — import your own connections"),
     ("contacts-retention", "contacts · Objections and retention — the privacy sweep")],
    phase=4, cols=2)

screen_slide("PART 1 · APPLY", "Applications and the Apply browser",
    "The generated CV, briefing, motivation document and email for each shortlisted role — "
    "all for the job seeker to read, edit and approve.",
    ["A consistency check catches invented employers, dates and titles (FR-322).",
     "A leak scan refuses briefing and motivation as attachments (NFR-206).",
     "Approving authorises dispatch but sends nothing; messages go from the seeker's own "
     "mailbox.",
     "The job seeker is the last line of defence for anything technically true and still "
     "wrong for the audience."],
    "package-email", phase=4, caption_text="apply browser · the email, with every review tab")

montage_slide("PART 1 · APPLY", "The four artefacts, and the checks before dispatch",
    "CV, briefing, motivation and email are generated together; the briefing and motivation "
    "never leave the machine, and the checks gate the send.",
    [("package-cv", "apply · CV — grounded in the profile, nothing untraceable"),
     ("package-briefing", "apply · Briefing — for the job seeker, never attached"),
     ("package-motivation", "apply · Motivation — the longer statement, never attached"),
     ("package-checks", "apply · Checks — consistency and leak scan before dispatch")],
    phase=4, cols=2)

screen_slide("PART 1 · APPLY", "Networking and export",
    "Warm introduction routes, an event radar with calendar export, and a self-contained "
    "campaign package for a coach — built from one structure, redacted like the LLM path.",
    ["Introduction routes are ranked by strength and relevance (FR-461).",
     "Events come with a match meter, calendar links and an .ics download (FR-462).",
     "The export is a PDF bundle plus machine-readable JSON, and it refuses outright if it "
     "could reach another seeker's data (FR-463).",
     "Discretion mode is honoured on every response (FR-385)."],
    "networking-events", phase=4, caption_text="networking · Event radar and the export builder")

# ===========================================================================
# FOLLOW UP
# ===========================================================================
screen_slide("PART 1 · FOLLOW UP", "Application pipeline",
    "Every authorised application and its stage, from sent to reply, interview and "
    "outcome, on one board.",
    ["Open a card for the classified reply, the drafted answer and the stage dates.",
     "Approving a draft queues it for the mail screen; it still sends nothing.",
     "Outcomes feed back into generation defaults and scoring weights.",
     "The negotiation brief and a mock interview sit on the same card."],
    "pipeline", phase=5, caption_text="pipeline · the five-stage board")

screen_slide("PART 1 · FOLLOW UP", "Responses received",
    "Every answer to an application, however it arrived: detected automatically from "
    "Gmail, or recorded by hand for a call, a portal or a LinkedIn message.",
    ["Recording rejections matters as much as good news; both feed the pattern analysis.",
     "An application counts as answered or as silence, and nothing in between until 21 "
     "days pass.",
     "Recording a response moves the card on the pipeline board automatically.",
     "A wrong outcome is correctable, because it distorts every rate it is counted in."],
    "responses", phase=5, caption_text="responses · record, correct and review")

screen_slide("PART 1 · FOLLOW UP", "What works, and where to redirect",
    "Which kinds of job and company actually answer, computed from recorded outcomes and "
    "always shown with the sample size behind them.",
    ["Rates are drawn with a plausible range, so a thin segment cannot look decisive.",
     "Advice needs roughly six resolved applications before it will say anything.",
     "Applying a proposal creates a new directive version — it never overwrites (FR-285).",
     "The data does not get to overrule the dream job; it only shows what the mismatch costs."],
    "insights", phase=5, caption_text="insights · outcome rates with Wilson intervals")

# ===========================================================================
# SYSTEM
# ===========================================================================
screen_slide("PART 1 · SYSTEM", "Monitoring",
    "The operational surface: campaign health, token spend, extraction rates and the "
    "audit trail, plus the scheduled re-checks of watched companies.",
    ["Extraction-rate monitoring surfaces adapter breakage (NFR-403).",
     "Every LLM call, egress request and score is audited with its provenance.",
     "Watched companies are rechecked on a schedule, and what changed becomes one weekly "
     "digest (FR-401/403).",
     "Notifications, timing windows and the scheduler are all on this screen."],
    "monitoring", phase=0, caption_text="monitoring · watchlist, digests and the scheduler")

screen_slide("PART 1 · SYSTEM", "Mail setup",
    "Two ways to send from the seeker's own identity — Gmail OAuth or a Resend relay — "
    "behind one dispatcher that holds every guard rail.",
    ["Gmail and Resend both satisfy 'send from your own mailbox' (FR-325).",
     "Sending rules, windows and a daily cap protect sender reputation (NFR-702).",
     "The dispatch log records everything sent, down to the message id; replies are detected.",
     "Follow-ups become due on a schedule and are drafted, then approved, never auto-sent "
     "(FR-327)."],
    "mail-mailboxes", phase=0, caption_text="mail setup · mailboxes, rules, log and follow-ups")

screen_slide("PART 1 · SYSTEM", "Administration",
    "The installation-level controls: the source catalogue and its terms acknowledgements, "
    "the server logs, model routing and the data-rights tools.",
    ["Nine tabs: Users, Models, Sources, Activity, Continuous, Audit, Logs, Data, Employer "
     "kind.",
     "Sources that require acknowledgement stay disabled until an administrator confirms "
     "their terms (IR-101).",
     "Model routing chooses the provider per task and can point at a local endpoint for "
     "private data (FR-362, NFR-306).",
     "Two actions cannot be undone: the redaction sweep and erasing a job seeker; erasing "
     "leaves the shared market data standing (FR-108)."],
    "admin-sources", phase=0, caption_text="administration · the source catalogue and its terms")

montage_slide("PART 1 · SYSTEM", "Administration, the other eight tabs",
    "Activity and Continuous show what the installation has collected; Models controls "
    "spend and routing; Data holds the irreversible actions.",
    [("admin-models", "admin · Models — provider, routing, budget and usage"),
     ("admin-activity", "admin · Activity — corpus, traffic and cost counters"),
     ("admin-data", "admin · Data — retention, redaction and erasure"),
     ("admin-users", "admin · Users — accounts, admin role and password resets")],
    phase=0, cols=2)

screen_slide("PART 1 · SYSTEM", "Help, everywhere",
    "Every screen explains itself: inline '?' tips drawn from a 142-term glossary, a "
    "per-screen drawer with purpose, steps and cautions, and first-run guidance that "
    "works before any data exists.",
    ["Press ? anywhere to open the drawer for the screen you are on.",
     "The drawer carries purpose, how to work through it, worth knowing, and a caution "
     "where reliance is risky.",
     "Inline tips resolve from the same glossary, so a term means the same thing "
     "everywhere.",
     "Help is static and offline: it works before a profile exists."],
    "help", phase=0, caption_text="help drawer · opened with the ? key on any screen")

# ===========================================================================
# PART 2 · ARCHITECTURE
# ===========================================================================
sl = new_slide(2)
tf = textbox(sl, 0.9, 2.35, 11.0, 0.5)
para(tf, "PART 2", 14, PHASE[2], bold=True, first=True)
tf = textbox(sl, 0.9, 2.7, 11.3, 1.0)
para(tf, "Technical architecture", 34, INK, bold=True, first=True)
tf = textbox(sl, 0.9, 3.9, 9.8, 1.4)
para(tf, "The layered system, the five chokepoints that make its requirements "
         "enforceable, and the path one campaign takes through the pipeline.", 13.5, INK2, first=True)
footer(sl, N)

figure_slide("ARCHITECTURE · SHAPE", "The shape of the system",
    "A React single-page application over a FastAPI API, a pipeline of stages, and five "
    "services — LLMClient, EgressClient, SourceAdapter, JobRunner and crypto — over a "
    "repository layer and SQLite.",
    "ta-01-architecture", phase=2,
    caption_text="422 routes over 88 tables · 227 Python modules, 140 JS modules · 2,061 tests.")


figure_slide("ARCHITECTURE · CHOKEPOINTS", "The five chokepoints",
    "The architecture funnels five concerns through exactly one module each, which is what "
    "makes the requirements below them enforceable rather than aspirational.",
    "ta-02-chokepoints", phase=2,
    notes=["Every SQL statement — db/repositories/*, db/connection.py: portability and "
           "isolation (CR-408, FR-344).",
           "Every outbound HTTP request — egress/client.py: robots, pacing and raw capture "
           "(FR-182/183).",
           "Every LLM call — llm/client.py: budget, injection defence, redaction, routing, "
           "audit.",
           "Every external source — adapters/base.py: one file per source, terms status "
           "(NFR-601, IR-101).",
           "Every long-running task — jobs/runner.py: pause, cancel, resume (FR-185, "
           "NFR-401)."])

figure_slide("ARCHITECTURE · DATAFLOW", "The path of one campaign",
    "From directives to redirection advice. The middle of the flow reads and writes the "
    "shared knowledge base; replies stay private to one job seeker.",
    "fdd-05-planning", phase=2,
    notes=["Plan: directives and the composite profile become a priced, per-source plan "
           "with a human gate before collection (FR-161..166).",
           "Reuse: the planner asks the knowledge base what is fresh enough and collects "
           "only the rest (FR-342/343).",
           "Collect: the plan runs through the adapters and EgressClient into raw documents "
           "and knowledge-base rows (FR-181..186).",
           "Analyse and rank: companies, filings, opportunities, compensation and seven "
           "sub-scores produce the advisory ranking (FR-221..285).",
           "Apply and learn: contacts, documents, dispatch, replies and redirection advice "
           "that becomes the next directives (FR-301..285)."])

table_slide("ARCHITECTURE · STAGES", "One campaign, stage by stage",
    "Every stage persists its own artefacts and can be re-run on its own (NFR-603). The "
    "user-facing pipeline is the five phases: Profile, Plan, Discover, Apply, Follow up.",
    ["Stage", "What it produces", "Requirements"],
    [["Plan", "Campaign plan: sources, queries, cost", "FR-161..166"],
     ["Reuse", "What the knowledge base already has, fresh enough", "FR-342/343"],
     ["Collect", "Raw documents through the adapters and egress", "FR-181..185"],
     ["Profile", "Shared company profiles", "FR-221..225"],
     ["Financials", "Five-year analysis, ability to pay, investment capacity", "FR-241..246"],
     ["Enrich", "Hiring signals, employer kind, domains", "FR-201..207"],
     ["Synthesise", "Opportunities, including speculative openings", "FR-261/262"],
     ["Price", "Compensation estimate against the stated minimum", "FR-264"],
     ["Score", "Seven sub-scores and the ranked list", "FR-281..285"],
     ["Contacts", "Hiring contacts, validated and ranked", "FR-302..306"],
     ["Generate", "CV, briefing, motivation and email", "FR-321..324"],
     ["Dispatch", "Send, approved and from your own mailbox", "FR-325..327"],
     ["Learn", "Replies, outcomes and redirection advice", "FR-285, FR-425"]],
    widths=[1.4, 7.6, 1.6], phase=2, font_size=9.5, row_height=0.335)

figure_slide("ARCHITECTURE · SOURCES", "The adapter contract",
    "One contract — plan, fetch, parse, normalise — and every source is one subclass and "
    "one decorator (NFR-601).",
    "ta-05-adapter", phase=2,
    notes=["plan: directives, composite profile and caps become this source's own query "
           "form, priced and paced.",
           "fetch: every request goes through EgressClient — robots, rate limit, cache and "
           "raw capture.",
           "parse: JSON-LD first, then deterministic selectors, then LLM extraction, in "
           "that order.",
           "normalise: knowledge-base columns carrying a confidence and a provenance record "
           "(NFR-402)."])

figure_slide("ARCHITECTURE · LLM", "The path of one LLM call",
    "Every call in the system takes this path, because llm/client.py is the only module "
    "that speaks to a model.",
    "ta-04-llm", phase=2,
    notes=["Redact, route, fence: do-not-disclose fields stripped, a model chosen per task, "
           "scraped text quarantined (CR-410, NFR-205).",
           "Check the budget pre-flight, then shed optional work rather than stop "
           "mid-campaign (NFR-104).",
           "Validate the response before any of it is used; injection defence is at the "
           "boundary.",
           "Debit and audit: tokens, cost, latency, model and prompt version recorded "
           "(FR-364)."])

figure_slide("ARCHITECTURE · EGRESS", "One outbound request, end to end",
    "No other module opens an HTTP connection. Cache, robots.txt, pacing, fetch, capture, "
    "parse — every response kept under its content hash (FR-183).",
    "ta-11-egress", phase=2,
    notes=["A cache hit never reaches the network.",
           "robots.txt is parsed per domain; a disallow raises rather than being ignored.",
           "A 429 or 503 backs off exponentially under one retry policy (IR-102).",
           "Raw bodies are stored on disk so an extractor can be re-run on the page it "
           "originally saw."])

figure_slide("ARCHITECTURE · DATA", "One database, three scopes",
    "49 private tables carry a job_seeker_id; 38 shared tables carry none; the migration "
    "ledger is the third. FR-344 is the invariant: no shared row links back to a job seeker.",
    "ta-03-schema", phase=2,
    notes=["Private: profile versions, campaigns, opportunities, applications, replies.",
           "Shared: companies, vacancies, financial years, signals, competitors, events.",
           "Restricted: contact straddles the boundary — browser-collected rows are private "
           "to the campaign that made them (NFR-303).",
           "Two FTS5 virtual tables index companies and vacancies; a usable_contact view "
           "encodes the objection rule at the database.",
           "Erasing a job seeker deletes the private rows and leaves the shared market data "
           "standing (FR-108)."])

figure_slide("ARCHITECTURE · JOBS", "Every long task is resumable",
    "A job runner is the single home for long tasks: jobs are pauseable, cancellable and "
    "restart-safe, and they never run on the request loop.",
    "ta-06-jobrunner", phase=2,
    notes=["Jobs run on their own threads with per-unit checkpoints (FR-185, NFR-401/502).",
           "A paused job resumes where it stopped, not from the beginning.",
           "All job threads use the bulk write lane, so requests never queue behind a "
           "campaign (NFR-102).",
           "Job outcomes, progress and errors are persisted and surfaced on screen."])

figure_slide("ARCHITECTURE · CODEBASE", "The codebase, measured",
    "The pipeline is the centre of gravity; the interface and the tests are the next two "
    "largest bodies of work. Numbers are measured, not estimated.",
    "ta-10-codebase", phase=2,
    caption_text="2,061 tests collected across 114 files; tests are 34% of the backend's lines.")

figure_slide("ARCHITECTURE · FRONTEND", "What the interface costs",
    "Route-level code splitting keeps the first paint small; the heaviest screen loads "
    "only when it is opened.",
    "ta-08-bundle", phase=2,
    notes=["Before splitting the bundle was 858 kB; the shared entry is now 312 kB, 105 kB "
           "over the wire.",
           "33 lazy chunks: a screen's code downloads when the route is opened (NFR-101).",
           "Every phase hue meets WCAG AA for small text, on white and on its own tint, in "
           "both themes.",
           "The screenshots in Part 1 are the running application, not mock-ups."])

# ===========================================================================
# QUALITY / CHARTS
# ===========================================================================
figure_slide("QUALITY · COVERAGE", "Requirement coverage",
    "156 of 157 requirements in the specification are cited in the implementation — "
    "99.4%; the one not cited is the DPIA (NFR-304), a document rather than code.",
    "fdd-11-coverage", phase=3,
    caption_text="Must 103/104, Should 44/44, Could 9/9. The line is not cited is NFR-304.")

figure_slide("QUALITY · SCORING", "Seven sub-scores, one explained rank",
    "Every rank is a weighted mean over seven named sub-scores; the weights are visible, "
    "editable and learned from outcomes, and the score orders a list rather than making a "
    "decision.",
    "fdd-07-scoring", phase=3,
    notes=["Profile fit 0.22, dream-job fit 0.22, directive fit 0.18, company 0.14, "
           "compensation 0.10, plausibility 0.08, reachability 0.06.",
           "Deterministic pre-rank of the whole corpus, then LLM scoring of the top 500 by "
           "default.",
           "The dream-job meter uses must/strong/nice importance and met/partial/violated "
           "status.",
           "Scores are advisory (NFR-305); manual order and pins win over the computed rank."])

figure_slide("QUALITY · LEARNING", "Learning it refuses to overstate",
    "Outcome rates are shown with a Wilson interval and the sample size, and the system "
    "will not advise below six resolved applications.",
    "fdd-10-sample-size", phase=5,
    notes=["Every rate carries its interval and its n, so a thin segment cannot look "
           "decisive.",
           "Advice needs roughly six resolved applications before it speaks (FDD 9.3).",
           "Applying a proposal writes a new directive-set version; nothing is auto-applied "
           "(NFR-305).",
           "Rejections are data too: they feed the same segmentation as interviews."])

figure_slide("ARCHITECTURE · FINANCIALS", "Two advisory scores from filed accounts",
    "Five years of filed figures are reduced to ability to pay and investment capacity — "
    "weighted, coverage-shaded, and never claiming more than the evidence supports.",
    "fdd-06-financial", phase=2,
    caption_text="A profile built from secondary signals rather than filings can never "
                 "score above 55 (FR-245).",
    notes=["Ability to pay: cost per FTE 0.35, EBIT margin 0.25, equity/assets 0.20, "
           "current ratio 0.20.",
           "Investment capacity: revenue CAGR 0.22, cash runway 0.22, headcount CAGR 0.18, "
           "EBITDA margin 0.18, debt/equity 0.12, capex/revenue 0.08.",
           "Components that cannot be computed are not invented; the score is shaded "
           "toward 0.5 for the missing coverage.",
           "Both scores are advisory and cite the figures they rest on (NFR-305, RK-06)."])

# ===========================================================================
# PART 3 · DATA SOURCES
# ===========================================================================
sl = new_slide(3)
tf = textbox(sl, 0.9, 2.35, 11.0, 0.5)
para(tf, "PART 3", 14, PHASE[3], bold=True, first=True)
tf = textbox(sl, 0.9, 2.7, 11.3, 1.0)
para(tf, "Data sources", 34, INK, bold=True, first=True)
tf = textbox(sl, 0.9, 3.9, 9.8, 1.4)
para(tf, f"Every adapter registered in the codebase — {F['adapters']} today — what each one "
         "covers, how it is reached, and whether it needs consent.", 13.5, INK2, first=True)
footer(sl, N)

# Group the catalogue.
def by_type(t):
    return [s for s in SOURCES if s["source_type"] == t]

ORDER = ["ats", "job_board", "registry", "directory", "news", "events", "website", "compensation"]
TYPE_LABEL = {
    "ats": "Applicant tracking systems", "job_board": "Job boards and public employment services",
    "registry": "Company registries", "directory": "Directories", "news": "News and signals",
    "events": "Conferences and events", "website": "Company websites",
    "compensation": "Compensation benchmarks",
}

def src_rows(types):
    rows = []
    for t in types:
        for s in by_type(t):
            tos = s["tos_status"]
            ack = "required" if s["requires_ack"] else "—"
            enabled = "yes" if s["effective_enabled"] else ("admin disabled" if s["enabled"] else "catalogue only")
            rows.append([s["display_name"], TYPE_LABEL[t], s["access_method"].upper(), tos, ack, enabled])
    return rows

COLS = ["Source", "Type", "Access", "Terms", "Acknowledgement", "Enabled"]
W = [3.3, 2.4, 1.1, 1.5, 1.8, 2.1]

# overview
sl = new_slide(3)
eyebrow(sl, "DATA SOURCES · OVERVIEW", 3)
title(sl, "The source catalogue at a glance")
lead(sl, f"{F['adapters']} adapters register themselves at import time. "
         f"{F['adapters_ack']} require an explicit terms acknowledgement before they can be "
         "enabled (IR-101).", y=1.3, size=12)
rows = []
for t in ORDER:
    items = by_type(t)
    ack = sum(1 for s in items if s["requires_ack"])
    rows.append([TYPE_LABEL[t], str(len(items)),
                 f"{ack} require acknowledgement" if ack else "—",
                 ", ".join(s["access_method"].upper() for s in items[:3]) + ("…" if len(items) > 3 else "")])
table(sl, 0.55, 2.0, 12.23, 4.6,
      ["Group", "Count", "Consent", "Access"],
      rows, widths=[3.4, 1.0, 3.0, 4.8], header_phase=3, font_size=11, header_size=11, row_height=0.48)
caption(sl, "Nothing is collected from a source whose terms are not acknowledged; the "
            "planner selects from this catalogue using the coverage each adapter declares "
            "(FR-161/164).", 0.55, 6.6, 12.2)
footer(sl, N)

table_slide("DATA SOURCES · ATS", "Applicant tracking systems",
    "Structured job feeds hosted by the employer's own recruiting system. Machine-readable, "
    "so collection costs no tokens.",
    COLS, [r for r in src_rows(["ats"])], W, phase=3, font_size=9.5, row_height=0.42)

table_slide("DATA SOURCES · JOB BOARDS", "Job boards and public employment services",
    "Advertised vacancies across Belgium, the Netherlands, the EU and globally. Terms "
    "status decides whether a source is enabled by default.",
    COLS, [r for r in src_rows(["job_board"])], W, phase=3, font_size=9.5, row_height=0.42)

table_slide("DATA SOURCES · REGISTRIES AND MORE",
    "Registries, directories, news, events, websites, compensation",
    "The non-vacancy sources: statutory filings that price a company, news and events that "
    "signal hiring, the company's own site, and the pay benchmark.",
    COLS, [r for r in src_rows(["registry", "directory", "news", "events", "website", "compensation"])],
    W, phase=3, font_size=8.6, row_height=0.365)

bullets_slide("DATA SOURCES · COVERAGE", "What the catalogue implies",
    "The catalogue is not a fixed list: the planner selects from it using the coverage "
    "metadata each adapter declares (FR-161/164).",
    ["Coverage is declared per adapter, so a Belgian search runs the registries and boards "
     "that cover Belgium and skips the rest.",
     "Terms status is first-class: SmartRecruiters, Indeed, StepStone and Meetup/Eventbrite "
     "require acknowledgement before they are enabled (IR-101).",
     "Adapters that return machine-readable payloads set llm_fallback = false, so the "
     "structured forms are read deterministically at zero token cost.",
     "Every source is reached through EgressClient, so robots.txt, pacing and raw capture "
     "apply no matter which adapter asked.",
     "The shared knowledge base is reused while fresh, so a second search over the same "
     "companies costs little or nothing (FR-342)."],
    phase=3, columns=2, size=11.5)

# ===========================================================================
# SUMMARY / CLOSING
# ===========================================================================
sl = new_slide(0)
eyebrow(sl, "SUMMARY", 0)
title(sl, "The system in five sentences")
lead(sl, "Profile in, ranked and explained opportunities out, every message reviewed and "
         "sent by the job seeker.", y=1.28, size=12.5)
bullets(sl, [
    "Profile in: a LinkedIn export, a CV and a description of the job actually wanted, "
    "merged into one composite profile with every conflict surfaced.",
    "Plan and collect: structured directives become a per-source plan, reviewed and priced "
    "before anything runs, then executed through 29 registered adapters behind one contract.",
    "Analyse and rank: company profiles, five years of filings and seven sub-scores produce "
    "an explained, advisory ranking of advertised and speculative opportunities.",
    "Apply with consent at every gate: generated documents are checked, approved by the job "
    "seeker, and sent from the seeker's own mailbox — nothing leaves on its own.",
    "Follow up and learn: replies, outcomes and negotiation are captured; redirection "
    "advice becomes the next directive version, which the seeker accepts or dismisses.",
], 0.55, 2.0, 12.2, 4.8, size=12.5)
footer(sl, N)

sl = new_slide(0)
rect(sl, 0, 0, 13.333, 0.16, fill=PHASE[1])
rect(sl, 2.667, 0, 2.666, 0.16, fill=PHASE[2])
rect(sl, 5.333, 0, 2.666, 0.16, fill=PHASE[3])
rect(sl, 8.0, 0, 2.666, 0.16, fill=PHASE[4])
rect(sl, 10.667, 0, 2.666, 0.16, fill=PHASE[5])
tf = textbox(sl, 0.9, 2.6, 11.5, 1.2)
para(tf, "Dream Job  ·  v2", 40, INK, bold=True, first=True, space_after=8)
para(tf, "Every screen, every diagram and every count in this deck was taken from the "
         "running system in September 2026.", 15, INK2, space_after=0)
tf = textbox(sl, 0.9, 4.4, 11.5, 1.0)
para(tf, f"{F['backend_files']} Python modules · {F['frontend_files']} JS modules · "
         f"{F['api_routes']} API routes · {F['db_tables']} tables · {F['adapters']} adapters · "
         f"{F['tests_passing']:,} tests passing", 12, INK3, bold=True, first=True)
footer(sl, N)

prs.save(OUT)
print(f"wrote {OUT} with {N} slides")
