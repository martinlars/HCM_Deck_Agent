"""
HCM Deck course agent — cross-origin OOPIF rewrite.
====================================================

One-job agent: navigate an LMS course and complete it in-place.

Design rules:

1. Stay on the same tab. Never reload, never close, never go_back, never relogin.

2. Cross-origin SCORM iframes are reached through OOPIF CDP targets (each iframe
   in a different origin gets its own CDP session via auto-attach), so all DOM
   tools work even when the iframe is cross-origin.

3. When the SCORM player is stuck, the agent does NOT restart. It calls
   `page_navigation_options` to enumerate alternative navigation on the SAME
   page (sidebar items, module list, breadcrumbs, "Pomiń"/"Następna lekcja"
   links) and clicks one of them.

4. Hard anti-loop. Repeating the SAME tool with the SAME args 3+ times in a row
   returns a HARD_ANTI_LOOP error string (not a nudge). The agent must switch
   tools.

5. Judge LLM disabled (was source of HTTP buffer overflow); output schema has
   strict size limits.

Usage:
  python agents\\course_agent.py --url "<course or dashboard URL>"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# --- Config ------------------------------------------------------------------

DEFAULT_CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_USER_DATA = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")

CHROME_EXE = os.path.expandvars(os.getenv("CHROME_EXE", DEFAULT_CHROME_EXE))
USER_DATA = os.path.expandvars(os.getenv("CHROME_USER_DATA", DEFAULT_USER_DATA))

AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")

DATA_DIR = Path("data")
RESULT_PATH = DATA_DIR / "course_run_summary.json"
# Disk-backed progress checkpoint. Persists per-course completion across a long
# 3000-step run so a mid-run crash never wipes finished-course state. Read once
# at startup, written every time the agent calls mark_course_done.
COMPLETED_PATH = DATA_DIR / "completed_courses.json"

# HCM Deck dashboard home — agent's landing URL. Configure per organization
# via the HCM_DASHBOARD_URL env var (e.g. "https://<tenant>.hcmdeck.com/protected/home")
# or via the --url CLI argument. The agent handles SSO redirect from this page.
DEFAULT_DASHBOARD_URL = os.getenv("HCM_DASHBOARD_URL", "").strip()

EXCLUDED_ACTIONS = [
    "close",
    "switch",
    "go_back",
    "search",
    "save_as_pdf",
    "write_file",
    "replace_file",
    "read_file",
    "upload_file",
]


# --- Schema ------------------------------------------------------------------


class CourseResult(BaseModel):
    course_name: str = Field(description="Visible course name", max_length=120)
    course_url: str = Field(description="Course URL or path", max_length=300)
    status: str = Field(
        description="Course status: SUCCESS | PARTIAL_SUCCESS | ALREADY_DONE | PLATFORM_LIMIT | FAILED",
        max_length=40,
    )
    score_percent: float = Field(description="Final score percentage (0-100)")
    notes: str = Field(default="", description="Short execution notes (<= 200 chars)", max_length=200)


class CourseRunSummary(BaseModel):
    entry_url: str = Field(description="Dashboard or starting URL used by the agent", max_length=300)
    status: str = Field(
        description="Global run status: SUCCESS | PARTIAL_SUCCESS | PLATFORM_LIMIT | BLOCKED_LOGIN | FAILED",
        max_length=40,
    )
    courses_total: int = Field(description="Total pending courses detected", ge=0)
    courses_completed: int = Field(description="Courses completed in this run", ge=0)
    average_score_percent: float = Field(description="Average score across courses (0-100)")
    target_score_percent: float = Field(description="Target score set by user")
    blocking_reason: str = Field(default="", description="Short reason if run could not continue", max_length=300)
    processed_courses: list[CourseResult] = Field(
        default_factory=list, description="Per-course results", max_length=120
    )
    actions_taken: list[str] = Field(
        default_factory=list, description="Short bullet list of key actions (<= 20 entries)", max_length=20
    )
    next_best_action: str = Field(
        default="", description="Best next step if target was not reached", max_length=300
    )


# --- Prompt ------------------------------------------------------------------


def build_task(course_url: str, target_score: int) -> str:
    return (
        f"You are a VISION-FIRST automation agent on HCM Deck (SCORM e-learning).\n"
        f"Start at: {course_url}\n"
        f"GOAL: log in via SSO, then COMPLETE EVERY TEST/COURSE/QUIZ available to the\n"
        f"user across the WHOLE platform (every section, every tab — not only the 'to-do'\n"
        f"list) with at least {target_score}% score on each. Keep going until every test\n"
        f"reachable from the dashboard has been attempted and scored.\n\n"
        "HARD RULES (NEVER violate):\n"
        "- One tab only. NEVER reload, NEVER close, NEVER open new, NEVER go_back.\n"
        "- SSO RECOVERY ALLOWED: SSO is normally active. If you land on a Microsoft/SSO\n"
        "  login page (login.microsoftonline.com or hcmdeck auth/realms), CLICK the\n"
        "  'Sign in with Microsoft' / 'Zaloguj' / 'Sign in' button — Chrome has cached\n"
        "  credentials, so a single click triggers automatic redirect back. NEVER type\n"
        "  passwords; just click the SSO button and wait for redirect (5-10 seconds).\n"
        "- If 'Czy chcesz wznowić poprzednią sesję?' appears, click 'Tak' first.\n"
        "- If your last 3 actions had identical (name, args), STOP and switch tool.\n"
        "- If stuck on the same screen for 3 distinct snapshot signatures: switch to"
        " vision_locate_and_click (works through nested cross-origin iframes), then to"
        " page_navigation_options.\n"
        "- Before starting a new course, ALWAYS call get_completed_courses to avoid\n"
        "  re-doing one you already finished in this run. After a course reaches its\n"
        "  final score, call mark_course_done(url, name, score).\n\n"
        "PLATFORM-WIDE COVERAGE PLAYBOOK (do this FIRST, before opening any test):\n"
        "A. Land on the HCM Deck dashboard home URL (passed in --url or set via\n"
        "   HCM_DASHBOARD_URL env var; typically '/protected/home' on the tenant).\n"
        "   Handle SSO redirect if needed.\n"
        "B. Call enumerate_platform_catalog — it discovers every section/tab the user\n"
        "   has (e.g. 'Do zrobienia', 'W trakcie', 'Zakończone', 'Katalog',\n"
        "   'Wszystkie szkolenia', 'Moje szkolenia', 'Ścieżki rozwoju', 'Wymagane')\n"
        "   and returns click coordinates + URL paths for each.\n"
        "C. For EACH discovered section in this order:\n"
        "     1) Do zrobienia / Wymagane (highest priority — usually mandatory)\n"
        "     2) W trakcie / Trwające (resume in-progress)\n"
        "     3) Katalog / Wszystkie szkolenia (broaden to ALL courses on platform)\n"
        "     4) Zakończone (only re-take if the platform allows it AND score < target)\n"
        "   For each section: click its coord, then call gather_course_cards to list\n"
        "   every test/course tile visible. Page through pagination if any.\n"
        "D. Build a working queue of unique (course_name, course_url) and process them\n"
        "   one by one. Skip any course present in get_completed_courses().\n\n"
        "PER-COURSE PLAYBOOK (HCM Deck has nested cross-origin SCORM iframes —"
        " runLessonContainer > lessonFrame at cs-12.hcmdeck.com — DOM tools may not see"
        " inner controls; vision_locate_and_click bypasses this):\n"
        "1. scorm_state -> read top_text, next_enabled, hotspot_count, frames.\n"
        "2. SEE the screen via screenshot. If a NASTĘPNY/DALEJ button is visually present:\n"
        "   a) try natural click first if scorm_state.next_enabled=true,\n"
        "   b) if no DOM result OR next_enabled=null: vision_locate_and_click(\"the NASTĘPNY"
        " button in bottom-right of the SCORM iframe\").\n"
        "   c) IF NEXT visible but NOT advancing after 2-3 clicks (slide unchanged):\n"
        "      call scorm_wait_for_ready(60) — content slides have time-gating, audio/video\n"
        "      must finish before NEXT works. Then click NEXT again.\n"
        "3. For interactive hotspots (numbered circles, map markers, image areas):\n"
        "   a) scorm_explore_hotspots once,\n"
        "   b) if no progress: vision_locate_and_click(\"hotspot number 1\"), then 2, 3...\n"
        "4. QUIZ / TEST HANDLING (this is the core of 'wszystkie testy'):\n"
        "   a) vision_describe_screen(\"the quiz question and ALL answer options\") to read.\n"
        "   b) Pick answer: vision_locate_and_click(\"answer option Tak\" / \"answer A\").\n"
        "      For radio buttons, click the TEXT LABEL next to the radio (not the tiny dot).\n"
        "   c) vision_locate_and_click(\"the SPRAWDŹ / WYŚLIJ submit button\").\n"
        "   d) vision_describe_screen(\"feedback after submit: Poprawnie or Niepoprawnie?\").\n"
        "   e) IF Niepoprawnie: pick OTHER answer (Tak↔Nie) or next option (A→B→C→D), submit.\n"
        "      Max 4 retries; for multi-choice >2 options try each systematically.\n"
        "   f) IF NO feedback at all: try clicking text label instead of radio dot;\n"
        "      try page_navigation_options to skip to a different module.\n"
        "   g) After Poprawnie: click NASTĘPNY to advance.\n"
        "5. After every action verify with scorm_state — if signature unchanged, use"
        " vision_describe_screen to see what's on screen, then switch strategy"
        " (different element / different tool / page_navigation_options).\n"
        "6. On apparent end of SCORM (e.g. 'Bye!', 'End of module', final score screen):\n"
        "   STOP — this is NOT enough to mark SUCCESS. Multi-module courses (Inclusive\n"
        "   Language has 3 modules, Rules of feedback has 5 sections, Working remotely\n"
        "   has multiple modules) show 'End of module 1' but the COURSE is only 33%/20%\n"
        "   complete. You MUST follow the COMPLETION VERIFICATION PROTOCOL below.\n"
        "7. If a course/test is non-completable (PLATFORM_LIMIT) after exhaustive in-place\n"
        "   attempts, record its status as PLATFORM_LIMIT via mark_course_done and MOVE ON\n"
        "   to the next queued course — do not block the rest of the run.\n\n"
        "COMPLETION VERIFICATION PROTOCOL (MANDATORY before every mark_course_done(SUCCESS)):\n"
        "  V.1  After the SCORM player shows 'Bye!' / 'End' / final score, return to the\n"
        "       dashboard / course catalog tab — use page_navigation_options to find a\n"
        "       breadcrumb or close-button, or vision_locate_and_click('back to catalog').\n"
        "  V.2  Call verify_course_completion('<course name>'). It reads the REAL\n"
        "       progress badge (%, 'Ukończone', 'Zakończone', progress bar aria-valuenow).\n"
        "  V.3  Interpret the verdict:\n"
        "        - VERIFIED_COMPLETE (pct >= 100 OR badge='completed') -> OK to call\n"
        "          mark_course_done(..., status='SUCCESS', verified=True).\n"
        "        - NOT_COMPLETE (pct < 100, e.g. 50/75%) -> there are more modules.\n"
        "          Re-open the course; use page_navigation_options or scorm_state.frames\n"
        "          to find the TOC/sidebar; click each module/section that does NOT have\n"
        "          a ✓ visited marker; then re-verify.\n"
        "        - NOT_FOUND -> the catalog isn't visible; navigate back to dashboard first.\n"
        "  V.4  mark_course_done(status='SUCCESS', verified=False) WILL BE REJECTED.\n"
        "       This is intentional — it prevents the false-SUCCESS bug from the previous\n"
        "       run where 3 multi-module courses were marked done after only 1 module.\n"
        "  V.5  For non-success outcomes (PLATFORM_LIMIT / PARTIAL_SUCCESS / FAILED) you\n"
        "       do NOT need verify_course_completion — verified arg is ignored. Use these\n"
        "       statuses honestly when a course is stuck.\n\n"
        "INTERACTIVE STALL ESCAPE PLAYBOOK (use IMMEDIATELY when stuck on a slide):\n"
        "Trigger: after 2-3 actions on the same slide the snapshot signature has NOT\n"
        "changed (scorm_state.top_text / hotspot_count / next_enabled all same). The\n"
        "system also auto-blocks normal tools after 8 calls without sig change\n"
        "(HARD_ANTI_LOOP_STAGNATION). When that happens, run THIS sequence:\n"
        "  ESC.1  scorm_brute_grid_click(rows=6, cols=4) — covers most hotspot/canvas\n"
        "         interactives that DOM cannot enumerate. Check scorm_state after.\n"
        "  ESC.2  scorm_keypress('Space') — many SCORM authoring tools accept Space as\n"
        "         'acknowledge slide read'. Then try 'Enter', then 'ArrowRight',\n"
        "         then 'PageDown'. ONE key per call, check scorm_state between.\n"
        "  ESC.3  IF you see draggable cards / match-pairs / sortable items on screen:\n"
        "         scorm_drag_drop_probe(max_pairs=6). Useful for SCORM 'drag the label\n"
        "         to the right column' interactives.\n"
        "  ESC.4  scorm_dismiss_overlay — there may be a hidden tooltip overlay catching\n"
        "         clicks (especially after a wrong answer).\n"
        "  ESC.5  page_navigation_options — find sidebar / TOC / modules_list jump to\n"
        "         the NEXT page or section. Click_coordinates on it.\n"
        "  ESC.6  vision_describe_screen('what specifically is required on this slide?')\n"
        "         — sometimes the user must drag, swipe, type, or hover; the visual\n"
        "         description reveals what the interactive expects.\n"
        "  ESC.7  If 6 escape attempts all fail: mark_course_done(status='PLATFORM_LIMIT')\n"
        "         and immediately move to the next queued course. Do NOT keep retrying.\n\n"
        "TOOLS:\n"
        "  Discovery:        enumerate_platform_catalog, gather_course_cards,\n"
        "                    page_navigation_options\n"
        "  Completion check: verify_course_completion(course_name) [MANDATORY before\n"
        "                    mark_course_done(SUCCESS)]\n"
        "  Session memory:   get_completed_courses,\n"
        "                    mark_course_done(course_url, course_name, score_percent,\n"
        "                    status, verified)\n"
        "  SCORM:            scorm_state, scorm_force(target='next'|'quiz'),\n"
        "                    scorm_explore_hotspots(max_clicks), scorm_dismiss_overlay,\n"
        "                    scorm_wait_for_ready(max_seconds)\n"
        "  Escape (stall):   scorm_brute_grid_click(rows,cols), scorm_keypress(key),\n"
        "                    scorm_drag_drop_probe(max_pairs)\n"
        "  Vision:           vision_locate_and_click(description),\n"
        "                    vision_describe_screen(question)\n"
        "  Plus standard click/screenshot.\n\n"
        "LEARN FROM SUCCESS: when an action advances state, the next time you see a"
        " similar slide (same top_text head), prefer the same tool+args.\n\n"
        "STOP CONDITION: stop only when get_completed_courses() covers EVERY course\n"
        "discovered by enumerate_platform_catalog + gather_course_cards across every\n"
        "section, OR when the step budget runs out.\n\n"
        "OUTPUT: call done with CourseRunSummary. notes <= 200 chars per course."
        " actions_taken <= 20 short bullets. processed_courses must list every course\n"
        " attempted in this run (success, partial, platform-limit, all of them).\n"
    )


# --- Tools registration ------------------------------------------------------


def register_custom_tools(tools, llm=None):
    """Attach SCORM-helper actions to a Tools instance.

    Tools (all OOPIF-aware):
      - scorm_state: rich snapshot merged across top + every iframe.
      - scorm_force(target='next'|'quiz'): DOM+coord fallback.
      - scorm_explore_hotspots(max_clicks): enumerate + click hotspots via CDP.
      - scorm_dismiss_overlay: close popup/tooltip overlay.
      - page_navigation_options: scan page for alternative navigation links.
      - vision_locate_and_click(description): VISION-FIRST. Take screenshot,
        ask LLM where the element is, click those coordinates. DOM-independent.
        Best for cross-origin/nested SCORM iframes.
    """

    from browser_use.browser.events import ClickCoordinateEvent, ScreenshotEvent

    # ---- Anti-loop closure state ----
    _action_log: list[str] = []
    _last_state_sig: dict[str, str] = {"sig": ""}
    # Counter of tool calls since the LAST observed snapshot-signature change.
    # Increments on every _check_loop call; resets to 0 inside scorm_state when
    # a new signature is detected. The old per-(tool,args) loop guard never
    # caught the Mobbing-Page-10/22 stall because the agent kept varying the
    # 'description' on vision_locate_and_click — every call looked unique so
    # the streak stayed at 1. This signature-based counter catches stagnation
    # REGARDLESS of which tool/args are used.
    _calls_since_sig_change: dict[str, int] = {"n": 0}
    # On stagnation, the agent gets told to use the escape-tools below. These
    # tools are exempt from stagnation block (they ARE the escape).
    _ESCAPE_TOOLS = {
        "page_navigation_options",
        "scorm_brute_grid_click",
        "scorm_keypress",
        "scorm_drag_drop_probe",
        "scorm_dismiss_overlay",
        "gather_course_cards",
        "enumerate_platform_catalog",
        "mark_course_done",
        "get_completed_courses",
        "verify_course_completion",
        "vision_describe_screen",
    }

    def _check_loop(action_name: str, args_repr: str) -> str | None:
        sig = f"{action_name}|{args_repr}"
        _action_log.append(sig)
        if len(_action_log) > 12:
            _action_log.pop(0)
        _calls_since_sig_change["n"] += 1

        # Guard 1: identical (tool, args) repeated ≥4 times in a row.
        streak = 0
        for s in reversed(_action_log):
            if s == sig:
                streak += 1
            else:
                break
        if streak >= 4:
            return (
                f"HARD_ANTI_LOOP: action '{action_name}' with args '{args_repr}' was called "
                f"{streak} times in a row with no state change. STOP. Switch tool: try "
                f"page_navigation_options to find an alternative path on this same page, or "
                f"scorm_dismiss_overlay, scorm_brute_grid_click, scorm_keypress, or "
                f"click_coordinates on a different region."
            )

        # Guard 2: ≥8 total tool calls since last snapshot-signature change.
        # This catches the Mobbing-style stall where the agent keeps changing
        # vision-click descriptions or coord clicks (each unique args) but the
        # SCORM slide hasn't actually moved. Escape tools are exempt.
        if (
            _calls_since_sig_change["n"] >= 8
            and action_name not in _ESCAPE_TOOLS
        ):
            return (
                f"HARD_ANTI_LOOP_STAGNATION: {_calls_since_sig_change['n']} tool calls "
                f"happened since the last snapshot signature change. The SCORM slide has "
                f"NOT advanced. STOP normal navigation. INTERACTIVE STALL ESCAPE: try in "
                f"order — (1) scorm_brute_grid_click (6x4 grid), (2) scorm_keypress with "
                f"keys 'Space', 'Enter', 'ArrowRight' one at a time, (3) scorm_drag_drop_probe "
                f"if you see draggable elements, (4) scorm_dismiss_overlay, "
                f"(5) page_navigation_options to find a sidebar / module-list jump to the "
                f"NEXT page or section. If none works after these, mark_course_done with "
                f"status=PLATFORM_LIMIT and move to next course."
            )
        return None

    # ---- Shared JS helpers (run in any document context) ----
    JS_HELPERS = r"""
      function norm(t) {
        return (t || "")
          .toLowerCase()
          .normalize("NFD")
          .replace(/[̀-ͯ]/g, "")
          .replace(/\s+/g, " ")
          .trim();
      }
      function isVisible(el) {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        if (!r || r.width < 2 || r.height < 2) return false;
        const win = (el.ownerDocument && el.ownerDocument.defaultView) || window;
        const s = win.getComputedStyle(el);
        if (!s) return false;
        if (s.display === "none" || s.visibility === "hidden" || s.pointerEvents === "none") return false;
        if (parseFloat(s.opacity || "1") < 0.05) return false;
        return true;
      }
      function isClickable(el) {
        if (!el || !isVisible(el)) return false;
        const win = (el.ownerDocument && el.ownerDocument.defaultView) || window;
        const s = win.getComputedStyle(el);
        if (s && s.cursor === "pointer") return true;
        const tag = (el.tagName || "").toLowerCase();
        if (tag === "button" || tag === "a" || tag === "area" || tag === "input") return true;
        if (el.hasAttribute && (el.hasAttribute("onclick") || el.hasAttribute("ng-click") || el.hasAttribute("data-hotspot"))) return true;
        const role = (el.getAttribute && el.getAttribute("role")) || "";
        if (role === "button" || role === "link" || role === "menuitem") return true;
        return false;
      }
      function bodyText(doc) {
        try { return (doc.body && doc.body.innerText) ? doc.body.innerText.slice(0, 1200) : ""; }
        catch (_) { return ""; }
      }
      function detectOverlays(doc) {
        try {
          const win = doc.defaultView || window;
          const all = Array.from(doc.querySelectorAll("*"));
          const overlays = [];
          for (const el of all) {
            if (overlays.length >= 4) break;
            if (!isVisible(el)) continue;
            const s = win.getComputedStyle(el);
            const z = parseInt(s.zIndex || "0", 10) || 0;
            const pos = s.position;
            const r = el.getBoundingClientRect();
            const cls = norm(el.className || "");
            const role = (el.getAttribute && el.getAttribute("role")) || "";
            const looksOverlay =
              (z >= 10 && (pos === "fixed" || pos === "absolute") && r.width > 80 && r.height > 60) ||
              cls.includes("modal") || cls.includes("popup") || cls.includes("tooltip") ||
              cls.includes("overlay") || cls.includes("dialog") ||
              role === "dialog" || role === "alertdialog";
            if (looksOverlay) {
              overlays.push({
                cls: cls.slice(0, 80), role: role,
                w: Math.round(r.width), h: Math.round(r.height), z: z,
                text: (el.textContent || "").trim().slice(0, 120)
              });
            }
          }
          return overlays;
        } catch (_) { return []; }
      }
      function detectNextButton(doc) {
        const sels = [
          "#links-right", ".links-right",
          "[id*='next' i]", "[class*='next' i]",
          "[aria-label*='nast' i]", "[aria-label*='dalej' i]",
          "[title*='nast' i]", "[title*='dalej' i]",
          "[data-action*='next' i]", "[data-direction='next']", "[data-testid*='next' i]",
          ".player-controls__next", ".scorm-next", ".btn-next", ".btn-forward",
          "[aria-label*='forward' i]", "[aria-label*='continue' i]",
          ".nav-next", ".navigation-next", "[role='button'][class*='right' i]",
          "button[class*='arrow-right' i]", "[class*='arrow_right' i]",
          ".controls-next, .controls__next, .player__next",
          "[class*='kontynuuj' i]"
        ];
        const seen = new Set();
        const cands = [];
        for (const s of sels) {
          let nodes = [];
          try { nodes = Array.from(doc.querySelectorAll(s)); } catch (_) {}
          for (const el of nodes) { if (!seen.has(el)) { seen.add(el); cands.push(el); } }
        }
        for (const el of Array.from(doc.querySelectorAll("button,a,div,span,[role='button'],li"))) {
          const t = norm(el.textContent || "");
          const a = norm((el.getAttribute && el.getAttribute("aria-label")) || "");
          const ti = norm((el.getAttribute && el.getAttribute("title")) || "");
          const tx = t || a || ti;
          if (!seen.has(el) && (
            tx === "nastepne" || tx === "nastepny" || tx === "dalej" || tx === "next" ||
            tx === "kontynuuj" || tx === "continue" || tx === "forward" ||
            tx === ">" || tx === "▶" || tx === "→" || tx === "►" ||
            tx.startsWith("nast") && tx.length < 12 ||
            tx.startsWith("dalej") && tx.length < 10
          )) {
            seen.add(el); cands.push(el);
          }
        }
        for (const el of cands) {
          if (!isVisible(el)) continue;
          const win = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const s = win.getComputedStyle(el);
          const disabledAttr = el.hasAttribute && el.hasAttribute("disabled");
          const ariaDisabled = (el.getAttribute && el.getAttribute("aria-disabled") === "true");
          const cls = norm(el.className || "");
          const looksDisabled =
            disabledAttr || ariaDisabled ||
            cls.includes("disabled") || cls.includes("inactive") ||
            (s && (s.pointerEvents === "none" || parseFloat(s.opacity || "1") < 0.4));
          return {
            text: (el.textContent || "").trim().slice(0, 60),
            enabled: !looksDisabled,
            cls: cls.slice(0, 80)
          };
        }
        return null;
      }
      function countHotspots(doc) {
        try {
          const all = Array.from(doc.querySelectorAll("button,a,area,div,span,svg,g,circle,polygon,rect,[role='button']"));
          let n = 0;
          for (const el of all) {
            if (!isClickable(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 12 || r.height < 12 || r.width > 600 || r.height > 600) continue;
            n++;
            if (n > 80) break;
          }
          return n;
        } catch (_) { return 0; }
      }
    """

    # Snapshot run inside a SINGLE document context (top-level OR an OOPIF).
    INNER_SNAPSHOT_JS = r"""
    (function () {
      __HELPERS__
      const out = {
        url: location.href,
        title: document.title || "",
        top_text: bodyText(document),
        nav_text: "",
        next_enabled: null,
        overlays: detectOverlays(document),
        hotspot_count: countHotspots(document),
        clickable_dump: []
      };
      const t = detectNextButton(document);
      if (t) { out.nav_text = t.text; out.next_enabled = t.enabled; }
      // Diagnostic dump of clickable elements (for unknown SCORM players).
      try {
        const seen = new Set();
        for (const el of Array.from(document.querySelectorAll("button,a,[role='button'],[role='link'],[onclick],[data-action]"))) {
          if (!isVisible(el)) continue;
          const t2 = (el.textContent || "").trim().slice(0, 30);
          const a = (el.getAttribute("aria-label") || "").slice(0, 30);
          const ti = (el.getAttribute("title") || "").slice(0, 30);
          const id = (el.id || "").slice(0, 20);
          const cls = (el.className || "").toString().slice(0, 40);
          const key = id + "|" + cls + "|" + t2 + "|" + a;
          if (seen.has(key)) continue;
          seen.add(key);
          out.clickable_dump.push({ tag: el.tagName.toLowerCase(), id: id, cls: cls, t: t2, al: a, ti: ti });
          if (out.clickable_dump.length >= 14) break;
        }
      } catch (_) {}
      return out;
    })()
    """.replace("__HELPERS__", JS_HELPERS)

    # Top-level extra info: list iframes seen at top, with rect (used to translate
    # iframe-local coords to viewport coords).
    TOP_IFRAMES_JS = r"""
    (function () {
      const out = [];
      for (const f of Array.from(document.querySelectorAll("iframe")).slice(0, 8)) {
        const r = f.getBoundingClientRect();
        out.push({
          src: (f.src || "").slice(0, 200),
          x: Math.round(r.left || 0),
          y: Math.round(r.top || 0),
          w: Math.round(r.width || 0),
          h: Math.round(r.height || 0)
        });
      }
      return out;
    })()
    """

    INNER_EXPLORE_JS = r"""
    (function (maxClicks) {
      __HELPERS__
      const candidates = [];
      const tags = "button,a,area,div,span,svg,g,circle,polygon,rect,image,[role='button'],[onclick],[data-hotspot],[data-target]";
      for (const el of Array.from(document.querySelectorAll(tags))) {
        if (!isClickable(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 12 || r.height < 12 || r.width > 600 || r.height > 600) continue;
        candidates.push({
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          w: Math.round(r.width),
          h: Math.round(r.height),
          tag: (el.tagName || "").toLowerCase(),
          text: (el.textContent || "").trim().slice(0, 60)
        });
      }
      const out = [];
      for (const c of candidates) {
        if (out.some((p) => Math.abs(p.x - c.x) < 30 && Math.abs(p.y - c.y) < 30)) continue;
        out.push(c);
        if (out.length >= maxClicks) break;
      }
      return { ok: true, points: out };
    })(__MAX__)
    """.replace("__HELPERS__", JS_HELPERS)

    INNER_DISMISS_JS = r"""
    (function () {
      __HELPERS__
      const sels = [
        "[aria-label*='close' i]",
        "[aria-label*='zamknij' i]",
        "[title*='close' i]",
        "[title*='zamknij' i]",
        ".modal-close, .popup-close, .close, button.close",
        "[class*='close-button' i]",
        "[data-action*='close' i]"
      ];
      for (const s of sels) {
        let nodes = [];
        try { nodes = Array.from(document.querySelectorAll(s)); } catch (_) {}
        for (const el of nodes) {
          if (!isVisible(el)) continue;
          try {
            for (const n of ["pointerdown","mousedown","pointerup","mouseup","click"]) {
              el.dispatchEvent(new MouseEvent(n, { bubbles: true, cancelable: true, view: window }));
            }
            try { el.click(); } catch(_) {}
            return { closed: true, selector: s };
          } catch (_) {}
        }
      }
      for (const el of Array.from(document.querySelectorAll("button,a,div,span"))) {
        const t = norm(el.textContent || "");
        if ((t === "x" || t === "✕" || t === "×" || t === "ok" || t === "rozumiem") && isVisible(el)) {
          try { el.click(); } catch (_) {}
          return { closed: true, selector: "text:" + t };
        }
      }
      return { closed: false };
    })()
    """.replace("__HELPERS__", JS_HELPERS)

    INNER_FORCE_JS = r"""
    (function (target) {
      __HELPERS__
      function fire(el) {
        if (!el) return false;
        try { el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" }); } catch (_) {}
        for (const n of ["pointerdown","mousedown","pointerup","mouseup","click"]) {
          try { el.dispatchEvent(new MouseEvent(n, { bubbles: true, cancelable: true, view: window })); } catch (_) {}
        }
        try { el.click(); } catch (_) {}
        return true;
      }
      function nextCandidates(doc) {
        const sels = [
          "#links-right", ".links-right",
          "[id*='next' i]", "[class*='next' i]",
          "[aria-label*='nast' i]", "[aria-label*='dalej' i]",
          "[aria-label*='forward' i]", "[aria-label*='continue' i]",
          "[title*='nast' i]", "[title*='dalej' i]",
          "[data-action*='next' i]", "[data-direction='next']", "[data-testid*='next' i]",
          ".player-controls__next", ".scorm-next", ".btn-next", ".btn-forward",
          ".nav-next", ".navigation-next",
          "button[class*='arrow-right' i]", "[class*='arrow_right' i]",
          ".controls-next, .controls__next, .player__next",
          "[class*='kontynuuj' i]"
        ];
        const seen = new Set(); const list = [];
        for (const s of sels) {
          let nodes = [];
          try { nodes = Array.from(doc.querySelectorAll(s)); } catch (_) {}
          for (const el of nodes) { if (!seen.has(el)) { seen.add(el); list.push(el); } }
        }
        for (const el of Array.from(doc.querySelectorAll("button,a,div,span,[role='button'],[onclick],li"))) {
          if (seen.has(el)) continue;
          const t = norm(el.textContent || "");
          const a = norm((el.getAttribute && el.getAttribute("aria-label")) || "");
          const ti = norm((el.getAttribute && el.getAttribute("title")) || "");
          const tx = t || a || ti;
          if (tx.includes("nastepne") || tx.includes("nastepny") || tx.includes("dalej") ||
              tx.includes("kontynuuj") || tx.includes("next") || tx.includes("continue") ||
              tx.includes("forward") || tx === ">" || tx === "▶" || tx === "→" || tx === "►") {
            seen.add(el); list.push(el);
          }
        }
        return list;
      }
      function quizOption(doc) {
        const visible = Array.from(doc.querySelectorAll("input,label,button,div,span,li,a,[role='button'],[role='radio']")).filter(isVisible);
        const radios = visible.filter((el) => {
          const tag = (el.tagName || "").toLowerCase();
          if (tag !== "input") return false;
          const tp = norm(el.getAttribute("type") || "");
          return tp === "radio" || tp === "checkbox";
        });
        if (radios.length) return radios[0];
        return visible.find((el) => {
          const t = norm(el.textContent || "");
          if (!t || t.length < 3) return false;
          if (t.includes("wyslij") || t.includes("submit") || t.includes("sprawdz") || t.includes("zatwierdz")) return false;
          if (t.includes("nastepne") || t.includes("dalej")) return false;
          const cls = norm(el.className || "");
          const id = norm(el.id || "");
          return t.length >= 5 || cls.includes("answer") || cls.includes("option") || cls.includes("radio") || id.includes("answer") || id.includes("option");
        }) || null;
      }
      function quizSubmit(doc) {
        return Array.from(doc.querySelectorAll("button,a,div,span,[role='button']")).filter(isVisible).find((el) => {
          const t = norm(el.textContent || "");
          const id = norm(el.id || "");
          const cls = norm(el.className || "");
          return t.includes("wyslij") || t.includes("submit") || t.includes("sprawdz") || t.includes("zatwierdz") || id.includes("submit") || cls.includes("submit");
        }) || null;
      }
      if (target === "next") {
        for (const el of nextCandidates(document)) {
          if (!isVisible(el)) continue;
          fire(el);
          return { clicked: true, mode: "next", text: (el.textContent || "").trim().slice(0, 80) };
        }
        return { clicked: false, mode: "next" };
      }
      const opt = quizOption(document);
      if (opt) fire(opt);
      const sub = quizSubmit(document);
      if (sub) fire(sub);
      if (opt || sub) {
        return {
          clicked: true, mode: "quiz",
          option: opt ? (opt.textContent || "").trim().slice(0, 80) : "",
          submit: sub ? (sub.textContent || "").trim().slice(0, 80) : ""
        };
      }
      return { clicked: false, mode: "quiz" };
    })(__TARGET__)
    """.replace("__HELPERS__", JS_HELPERS)

    NAV_OPTIONS_JS = r"""
    (function () {
      __HELPERS__
      function describe(el) {
        const r = el.getBoundingClientRect();
        const txt = ((el.innerText || el.textContent || "")
          + " " + (el.getAttribute('aria-label') || "")
          + " " + (el.getAttribute('title') || ""))
          .replace(/\s+/g, ' ').trim().slice(0, 80);
        const href = (el.getAttribute('href') || '').slice(0, 120);
        const role = (el.getAttribute('role') || el.tagName.toLowerCase());
        return {
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          tag: (el.tagName || '').toLowerCase(),
          role: role,
          text: txt,
          href: href,
          id: (el.id || '').slice(0, 40),
          cls: norm(el.className || '').slice(0, 80)
        };
      }
      const out = { sidebar:[], topnav:[], breadcrumbs:[], modules_list:[], skip_links:[], pagination:[], misc_buttons:[] };
      const KW_SKIP = ["pomin","skip","przejdz","nastepna lekcja","nastepny modul","kolejny","zakoncz lekcje","ukoncz","oznacz jako","mark complete"];
      const KW_BREAD = ["breadcrumb","sciezka","trail"];
      const KW_MOD = ["modul","module","lekcja","lesson","rozdzial","chapter","krok","step"];
      const sel = "a,button,[role='button'],[role='link'],[role='menuitem'],[role='tab'],li[role='option']";
      for (const el of Array.from(document.querySelectorAll(sel))) {
        if (!isVisible(el)) continue;
        const d = describe(el);
        const t = norm(d.text);
        const cls = d.cls;
        const role = d.role;
        if (cls.includes('sidebar') || cls.includes('side-menu') || cls.includes('nav-side') || cls.includes('side-nav')) out.sidebar.push(d);
        else if (cls.includes('breadcrumb') || KW_BREAD.some(k => cls.includes(k))) out.breadcrumbs.push(d);
        else if (KW_SKIP.some(k => t.includes(k))) out.skip_links.push(d);
        else if (KW_MOD.some(k => t.includes(k)) || cls.includes('lesson-list') || cls.includes('module-list') || cls.includes('lessons') || cls.includes('modules')) out.modules_list.push(d);
        else if (role === 'tab' || cls.includes('pagination') || /^(\d{1,3})$/.test(t)) out.pagination.push(d);
        else if (cls.includes('topbar') || cls.includes('header-nav') || cls.includes('top-nav')) out.topnav.push(d);
        else if (d.tag === 'a' && d.href) out.misc_buttons.push(d);
      }
      for (const k of Object.keys(out)) out[k] = out[k].slice(0, 8);
      return out;
    })()
    """.replace("__HELPERS__", JS_HELPERS)

    # ---- CDP helpers ----
    async def _eval_in(cdp_session, expression: str, context_id: int | None = None):
        """Evaluate JS in cdp_session, optionally scoped to executionContextId."""
        try:
            params = {"expression": expression, "returnByValue": True, "awaitPromise": True}
            if context_id is not None:
                params["contextId"] = context_id
            res = await cdp_session.cdp_client.send.Runtime.evaluate(
                params=params,
                session_id=cdp_session.session_id,
            )
            return res.get("result", {}).get("value")
        except Exception:
            return None

    _frame_diag = {"last_summary": ""}

    async def _all_frame_sessions(browser_session):
        """Return [(label, cdp_session, exec_ctx_id_or_None, frame_url), ...].

        Fix #2: dedup by frame_id (not target_id), so nested same-origin iframes
        sharing the parent's CDP target are still enumerated. For frames inside
        the same target, we resolve their executionContextId via Page domain so
        Runtime.evaluate can be scoped to the right document.
        """
        out: list[tuple] = []
        diag_lines = []
        try:
            top = await browser_session.get_or_create_cdp_session(focus=False)
        except Exception as e:
            _frame_diag["last_summary"] = f"top_err={e}"
            return out

        try:
            frames, _ = await browser_session.get_all_frames()
        except Exception as e:
            _frame_diag["last_summary"] = f"get_all_frames_err={e}"
            return [("top", top, None, "")]
        diag_lines.append(f"get_all_frames={len(frames)}")

        # Group frames by their owning CDP target.
        target_to_frames: dict[str, list[tuple[str, dict]]] = {}
        for fid, f in frames.items():
            ftid = f.get("frameTargetId") or f.get("targetId")
            if not ftid:
                continue
            target_to_frames.setdefault(ftid, []).append((fid, f))

        # Resolve executionContextId per (target, frame). For target's "main" frame
        # the contextId can be None — Runtime.evaluate then runs in default context
        # (i.e. that target's main document, which is what we want).
        attached = 0
        skipped_url = 0
        skipped_no_ctx = 0
        attach_errs: list[str] = []

        async def _ensure_runtime(cdp):
            try:
                await cdp.cdp_client.send.Runtime.enable(session_id=cdp.session_id)
            except Exception:
                pass

        for ftid, frame_list in target_to_frames.items():
            try:
                cdp = await browser_session.get_or_create_cdp_session(ftid, focus=False)
            except Exception as e:
                attach_errs.append(f"{ftid[:8]}:{type(e).__name__}")
                continue
            await _ensure_runtime(cdp)

            # Map frame_id -> executionContextId via Page.getFrameTree + handler hooks.
            ctx_map: dict[str, int] = {}
            try:
                tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
                # Collect all frame ids in this target (nested).
                stack = [tree.get("frameTree", {})]
                target_frame_ids: list[str] = []
                while stack:
                    node = stack.pop()
                    fr = (node or {}).get("frame") or {}
                    if fr.get("id"):
                        target_frame_ids.append(fr["id"])
                    for child in (node or {}).get("childFrames", []) or []:
                        stack.append(child)
                # Try to resolve a frame to a context via Runtime.executionContextCreated
                # event listing — fallback: createIsolatedWorld returns a contextId.
                for tfid in target_frame_ids:
                    try:
                        iso = await cdp.cdp_client.send.Page.createIsolatedWorld(
                            params={"frameId": tfid, "worldName": "course_agent_probe", "grantUniveralAccess": False},
                            session_id=cdp.session_id,
                        )
                        cid = iso.get("executionContextId")
                        if isinstance(cid, int):
                            ctx_map[tfid] = cid
                    except Exception:
                        pass
            except Exception:
                pass

            for fid, f in frame_list:
                url = (f.get("url") or "")
                if not url or url.startswith("chrome-error://") or url.startswith("about:blank") and len(frame_list) > 1:
                    skipped_url += 1
                    continue
                ctx = ctx_map.get(fid)
                if ctx is None and len(frame_list) > 1:
                    # Multiple frames in this target but we couldn't get exec ctx for this one.
                    # Use main context as fallback (will hit the parent doc; still useful for top frames).
                    skipped_no_ctx += 1
                    if fid == frame_list[0][0]:
                        # First frame: keep as main-target session anyway (legacy behaviour).
                        pass
                    else:
                        continue
                label = "top" if not out else f"frame[{url[:80]}]"
                out.append((label, cdp, ctx, url))
                attached += 1

        if not out:
            out = [("top", top, None, "")]

        diag_lines.append(
            f"attached={attached} skipped(url={skipped_url},no_ctx={skipped_no_ctx}) "
            f"attach_errs={attach_errs[:3]}"
        )
        _frame_diag["last_summary"] = " | ".join(diag_lines)
        return out

    async def _take_snapshot(browser_session) -> dict:
        sessions = await _all_frame_sessions(browser_session)
        merged = {
            "url": "",
            "title": "",
            "top_text": "",
            "nav_text": "",
            "next_enabled": None,
            "overlays": [],
            "hotspot_count": 0,
            "frames": [],
        }
        diag_internal: list[dict] = []  # logging-only, NOT returned to agent (saves tokens)
        eval_errs: list[str] = []
        for label, cdp, ctx, _url in sessions:
            data = await _eval_in(cdp, INNER_SNAPSHOT_JS, context_id=ctx)
            if not isinstance(data, dict):
                eval_errs.append(label[:30])
                continue
            cd = data.get("clickable_dump") or []
            if cd:
                diag_internal.append({"frame": label[:40], "samples": cd[:8]})
            if label == "top":
                merged["url"] = data.get("url", "")
                merged["title"] = data.get("title", "")
                merged["top_text"] = (data.get("top_text") or "")[:600]
                ov = data.get("overlays") or []
                if ov:
                    merged["overlays"] = ov
                merged["hotspot_count"] += int(data.get("hotspot_count") or 0)
                ne = data.get("next_enabled")
                if ne is not None and merged["next_enabled"] is None:
                    merged["next_enabled"] = ne
                    merged["nav_text"] = data.get("nav_text") or ""
            else:
                fdata = {
                    "label": label,
                    "url": (data.get("url") or "")[:120],
                    "next_enabled": data.get("next_enabled"),
                    "nav_text": (data.get("nav_text") or "")[:60],
                    "hotspot_count": int(data.get("hotspot_count") or 0),
                    "overlays": len(data.get("overlays") or []),
                    "text_head": (data.get("top_text") or "")[:240],
                }
                merged["frames"].append(fdata)
                merged["hotspot_count"] += fdata["hotspot_count"]
                if fdata["next_enabled"] is not None and merged["next_enabled"] is None:
                    merged["next_enabled"] = fdata["next_enabled"]
                    merged["nav_text"] = fdata["nav_text"]
                if fdata["overlays"] and not merged["overlays"]:
                    merged["overlays"] = [{"text": "(in iframe)", "label": label}]
        # Internal diagnostic log (not exposed to agent — saves tokens, prevents Invalid JSON: EOF).
        try:
            diag_path = DATA_DIR / "frame_diagnostics.jsonl"
            DATA_DIR.mkdir(exist_ok=True)
            with diag_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "frame_summary": _frame_diag.get("last_summary", ""),
                    "url": merged.get("url", ""),
                    "frames_seen": len(merged.get("frames") or []),
                    "next_enabled": merged.get("next_enabled"),
                    "hotspot_count": merged.get("hotspot_count"),
                    "eval_errs": eval_errs[:3],
                    "clickable_samples": diag_internal[:4],
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return merged

    def _signature(snap: dict) -> str:
        if not isinstance(snap, dict):
            return ""
        nav = (snap.get("nav_text") or "")[:60]
        ne = snap.get("next_enabled")
        hc = snap.get("hotspot_count", 0)
        ov = len(snap.get("overlays") or [])
        tt = (snap.get("top_text") or "")
        frames = snap.get("frames") or []
        fhead = ""
        if frames:
            fhead = "|".join(
                f"{f.get('label','')[:10]}:{f.get('next_enabled')}:{f.get('hotspot_count')}:{(f.get('text_head') or '')[:40]}"
                for f in frames[:3]
            )
        return f"nav={nav}|next={ne}|hot={hc}|ov={ov}|len={len(tt)}|head={tt[:60]}|f={fhead}"

    async def _click_coord(browser_session, x: int, y: int) -> bool:
        try:
            event = browser_session.event_bus.dispatch(
                ClickCoordinateEvent(coordinate_x=int(x), coordinate_y=int(y), force=True)
            )
            await event
            await event.event_result(raise_if_any=False, raise_if_none=False)
            return True
        except Exception:
            return False

    async def _top_iframes(browser_session) -> list[dict]:
        try:
            top = await browser_session.get_or_create_cdp_session(focus=False)
            data = await _eval_in(top, TOP_IFRAMES_JS)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # ---- Tools ----

    @tools.action(
        description=(
            "Read a compact SCORM/page snapshot merging top document + every iframe (incl. "
            "cross-origin OOPIFs). Returns JSON with: url, title, top_text, nav_text, "
            "next_enabled, overlays, hotspot_count, frames=[{label,url,next_enabled,"
            "nav_text,hotspot_count,overlays,text_head}]. Call BEFORE and AFTER significant "
            "actions to verify a real state change."
        )
    )
    async def scorm_state(browser_session) -> str:
        try:
            data = await _take_snapshot(browser_session)
            if not data:
                return "scorm_state: empty payload"
            new_sig = _signature(data)
            if new_sig and new_sig != _last_state_sig["sig"]:
                _last_state_sig["sig"] = new_sig
                _action_log.clear()
                _calls_since_sig_change["n"] = 0
            return json.dumps(data, ensure_ascii=False)[:1800]
        except Exception as exc:
            return f"scorm_state failed: {exc}"

    @tools.action(
        description=(
            "Force-click a SCORM control when normal click(index) does not work. "
            "target='next' clicks Polish 'NASTĘPNY' / 'DALEJ' / 'Kontynuuj' (DOM in top + each "
            "iframe; coord fallback aimed at iframe's bottom-right). "
            "target='quiz' selects the first visible answer and clicks 'WYŚLIJ'/submit."
        )
    )
    async def scorm_force(browser_session, target: str = "next") -> str:
        target = (target or "next").lower().strip()
        if target not in {"next", "quiz"}:
            return f"scorm_force: invalid target '{target}', expected one of next/quiz"
        guard = _check_loop("scorm_force", target)
        if guard:
            return guard
        try:
            expression = INNER_FORCE_JS.replace("__TARGET__", json.dumps(target))
            sessions = await _all_frame_sessions(browser_session)
            for label, cdp, ctx, _url in sessions:
                data = await _eval_in(cdp, expression, context_id=ctx)
                if isinstance(data, dict) and data.get("clicked"):
                    if target == "quiz":
                        return (
                            f"scorm_force[quiz]: option='{data.get('option', '')}' "
                            f"submit='{data.get('submit', '')}' frame={label}"
                        )
                    return f"scorm_force[next]: clicked '{data.get('text', '')}' frame={label}"

            iframes = await _top_iframes(browser_session)
            best = None
            for ifr in iframes:
                if (ifr.get("w") or 0) > 200 and (ifr.get("h") or 0) > 200:
                    if best is None or (ifr["w"] * ifr["h"]) > (best["w"] * best["h"]):
                        best = ifr
            if best is None:
                return f"scorm_force[{target}]: no DOM hit, no iframe rect for fallback"

            left = int(best["x"]); top = int(best["y"])
            w = int(best["w"]); h = int(best["h"])
            if target == "next":
                points = [
                    (left + w - 36, top + h - 28),
                    (left + w - 80, top + h - 28),
                    (left + w - 130, top + h - 28),
                    (left + w - 36, top + h - 60),
                ]
            else:
                points = [
                    (left + int(w * 0.26), top + int(h * 0.62)),
                    (left + int(w * 0.26), top + int(h * 0.74)),
                    (left + int(w * 0.74), top + int(h * 0.92)),
                ]
            clicked: list[str] = []
            for x, y in points:
                if await _click_coord(browser_session, x, y):
                    clicked.append(f"({x},{y})")
                    await asyncio.sleep(0.25)
            if clicked:
                return f"scorm_force[{target}]: coord fallback clicked {', '.join(clicked)}"
            return f"scorm_force[{target}]: no DOM, no coord fallback succeeded"
        except Exception as exc:
            return f"scorm_force[{target}] failed: {exc}"

    @tools.action(
        description=(
            "Discover ALL clickable hotspots in the page (top + every iframe incl. cross-origin "
            "OOPIFs) and click each via trusted CDP coordinate clicks. Captures a snapshot "
            "before and after each click; returns a per-hotspot diff. Use on intro/map/diagram "
            "screens where hotspots are not indexable."
        )
    )
    async def scorm_explore_hotspots(browser_session, max_clicks: int = 12) -> str:
        guard = _check_loop("scorm_explore_hotspots", str(max_clicks))
        if guard:
            return guard
        try:
            max_clicks = max(2, min(int(max_clicks or 12), 18))
            expression = INNER_EXPLORE_JS.replace("__MAX__", str(max_clicks))

            iframes = await _top_iframes(browser_session)

            def find_offset(label: str) -> tuple[int, int]:
                if label == "top":
                    return (0, 0)
                src = label.split("[", 1)[1].rstrip("]") if "[" in label else ""
                for ifr in iframes:
                    if ifr.get("src") and (src in ifr["src"] or ifr["src"] in src):
                        return (int(ifr.get("x") or 0), int(ifr.get("y") or 0))
                if iframes:
                    biggest = max(iframes, key=lambda f: (f.get("w") or 0) * (f.get("h") or 0))
                    return (int(biggest.get("x") or 0), int(biggest.get("y") or 0))
                return (0, 0)

            sessions = await _all_frame_sessions(browser_session)
            chosen_label = None
            chosen_points: list[dict] = []
            for label, cdp, ctx, _url in sessions:
                data = await _eval_in(cdp, expression, context_id=ctx)
                if isinstance(data, dict) and data.get("ok"):
                    pts = data.get("points") or []
                    if pts:
                        chosen_label = label
                        chosen_points = pts
                        break
            if not chosen_points:
                return "scorm_explore_hotspots: no hotspots discovered in any frame."

            ox, oy = find_offset(chosen_label)
            initial = await _take_snapshot(browser_session)
            initial_sig = _signature(initial)
            results = []
            changes = 0
            for i, p in enumerate(chosen_points, start=1):
                x = int(p["x"]) + ox
                y = int(p["y"]) + oy
                ok = await _click_coord(browser_session, x, y)
                await asyncio.sleep(0.6)
                snap = await _take_snapshot(browser_session)
                sig = _signature(snap)
                changed = sig != initial_sig
                if changed:
                    changes += 1
                    initial_sig = sig
                if i <= 6:
                    results.append(
                        f"#{i} ({x},{y}) tag={p.get('tag','')} text={(p.get('text') or '')[:24]!r} "
                        f"changed={changed} next_enabled={snap.get('next_enabled')}"
                    )
                if not ok and i <= 6:
                    results[-1] += " [click_dispatch_failed]"

            final = await _take_snapshot(browser_session)
            summary = (
                f"scorm_explore_hotspots[{chosen_label}]: clicked {len(chosen_points)}, "
                f"{changes} state changes. Final next_enabled={final.get('next_enabled')} "
                f"hotspot_count={final.get('hotspot_count')}.\n"
                + "\n".join(results)
            )
            return summary[:1500]
        except Exception as exc:
            return f"scorm_explore_hotspots failed: {exc}"

    @tools.action(
        description=(
            "Try to dismiss a popup/modal/tooltip overlay if scorm_state shows one. Sends "
            "Escape twice and clicks any visible close button (×, OK, 'Rozumiem', .close, etc.) "
            "in top OR any iframe (incl. cross-origin OOPIFs)."
        )
    )
    async def scorm_dismiss_overlay(browser_session) -> str:
        guard = _check_loop("scorm_dismiss_overlay", "")
        if guard:
            return guard
        try:
            try:
                cdp_session = await browser_session.get_or_create_cdp_session(focus=False)
                for _ in range(2):
                    await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                        params={"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
                        session_id=cdp_session.session_id,
                    )
                    await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
                        params={"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
                        session_id=cdp_session.session_id,
                    )
                    await asyncio.sleep(0.15)
            except Exception:
                pass

            sessions = await _all_frame_sessions(browser_session)
            for label, cdp, ctx, _url in sessions:
                data = await _eval_in(cdp, INNER_DISMISS_JS, context_id=ctx)
                if isinstance(data, dict) and data.get("closed"):
                    return f"scorm_dismiss_overlay[{label}]: closed via {data.get('selector')}"
            return "scorm_dismiss_overlay: no close-button matched (Escape was sent)"
        except Exception as exc:
            return f"scorm_dismiss_overlay failed: {exc}"

    @tools.action(
        description=(
            "Wait for SCORM content (audio/video/timeline) to finish playing across all "
            "iframes. Many SCORM slides have time-gated NEXT button: it stays disabled "
            "until audio/video reach end. Returns a JSON report: list of media elements "
            "found, their played/duration ratios, and whether all are 'ended'. Sleeps "
            "short intervals (max ~max_seconds total) and re-checks. USE THIS when "
            "scorm_state.next_enabled=true but clicking NEXT does NOT advance the slide "
            "across multiple attempts — likely time gating. Argument 'max_seconds' "
            "(default 60, max 180): total wait budget."
        )
    )
    async def scorm_wait_for_ready(browser_session, max_seconds: int = 60) -> str:
        guard = _check_loop("scorm_wait_for_ready", str(max_seconds))
        if guard:
            return guard
        try:
            max_seconds = max(5, min(int(max_seconds or 60), 180))
            INNER_MEDIA_JS = r"""
            (function () {
              const out = { audio: [], video: [], any_playing: false, all_ended: true };
              try {
                const els = Array.from(document.querySelectorAll("audio, video"));
                for (const el of els) {
                  const item = {
                    tag: el.tagName.toLowerCase(),
                    src: (el.currentSrc || el.src || "").slice(-60),
                    duration: Number(el.duration) || 0,
                    currentTime: Number(el.currentTime) || 0,
                    paused: !!el.paused,
                    ended: !!el.ended,
                    muted: !!el.muted,
                    autoplay: !!el.autoplay,
                    readyState: el.readyState
                  };
                  item.ratio = (item.duration > 0) ? Math.round((item.currentTime / item.duration) * 100) / 100 : null;
                  if (item.tag === 'audio') out.audio.push(item); else out.video.push(item);
                  if (!item.paused && !item.ended) out.any_playing = true;
                  if (!item.ended && item.duration > 0 && item.currentTime < item.duration - 0.5) out.all_ended = false;
                }
                if (els.length === 0) out.all_ended = false;  // no media -> we don't know
              } catch (e) { out.error = String(e).slice(0, 80); }
              return out;
            })()
            """

            async def _poll() -> dict:
                merged = {"audio": [], "video": [], "any_playing": False, "all_ended": True, "frames_with_media": 0}
                sessions = await _all_frame_sessions(browser_session)
                any_media = False
                for label, cdp, ctx, _url in sessions:
                    data = await _eval_in(cdp, INNER_MEDIA_JS, context_id=ctx)
                    if not isinstance(data, dict):
                        continue
                    a = data.get("audio") or []
                    v = data.get("video") or []
                    if a or v:
                        any_media = True
                        merged["frames_with_media"] += 1
                        merged["audio"].extend(a[:3])
                        merged["video"].extend(v[:3])
                    if data.get("any_playing"):
                        merged["any_playing"] = True
                    if a or v:
                        if not data.get("all_ended"):
                            merged["all_ended"] = False
                if not any_media:
                    merged["all_ended"] = False
                    merged["no_media_detected"] = True
                return merged

            start = asyncio.get_event_loop().time()
            poll_results: list[dict] = []
            check_interval = 2.0
            elapsed = 0.0
            last = await _poll()
            poll_results.append(last)

            if last.get("no_media_detected"):
                return (
                    "scorm_wait_for_ready: NO audio/video elements found in any frame. "
                    "Slide is likely NOT time-gated. Try other tactics: "
                    "vision_describe_screen('what is the gating requirement?'), "
                    "scorm_explore_hotspots, or page_navigation_options."
                )

            while elapsed < max_seconds and not last.get("all_ended"):
                await asyncio.sleep(check_interval)
                elapsed = asyncio.get_event_loop().time() - start
                last = await _poll()
                if elapsed >= max_seconds * 0.5:
                    check_interval = 3.0  # slow polling after halfway

            def _summary(item: dict) -> str:
                return f"{item['tag']}(t={item['currentTime']:.1f}/{item['duration']:.1f}s,ended={item['ended']},paused={item['paused']})"

            sample_items = (last.get("audio") or [])[:2] + (last.get("video") or [])[:2]
            sample_str = "; ".join(_summary(i) for i in sample_items)
            return (
                f"scorm_wait_for_ready: waited {elapsed:.1f}s. all_ended={last.get('all_ended')} "
                f"any_playing={last.get('any_playing')} frames_with_media={last.get('frames_with_media')}. "
                f"Media: {sample_str[:300]}. "
                f"{'OK to click NEXT now.' if last.get('all_ended') else 'Media still playing or timeout — try NEXT anyway, or wait longer.'}"
            )[:600]
        except Exception as exc:
            return f"scorm_wait_for_ready failed: {type(exc).__name__}: {exc}"

    @tools.action(
        description=(
            "VISION READ tool. Take a screenshot, ask the vision LLM the supplied "
            "QUESTION, return its short answer as text. USE THIS to: (a) read quiz "
            "questions and answer options, (b) check feedback after clicking SPRAWDŹ "
            "(correct vs incorrect), (c) understand what's currently on the slide, "
            "(d) see what changed after a vision_locate_and_click. "
            "Argument 'question': natural-language question about the screen "
            "(e.g. \"what is the quiz question and what are the answer options?\", "
            "\"is the feedback Poprawnie or Niepoprawnie?\", \"what slide title is shown?\", "
            "\"is there a 'Spróbuj ponownie' / Try again button visible?\")."
        )
    )
    async def vision_describe_screen(browser_session, question: str) -> str:
        guard = _check_loop("vision_describe_screen", question[:40])
        if guard:
            return guard
        if llm is None:
            return "vision_describe_screen: no llm instance bound"
        if not question or len(question.strip()) < 4:
            return "vision_describe_screen: question too short (need >= 4 chars)"
        try:
            from browser_use.llm.messages import (
                UserMessage, ContentPartTextParam, ContentPartImageParam, ImageURL,
            )
            try:
                ev = browser_session.event_bus.dispatch(ScreenshotEvent())
                await ev
                shot = await ev.event_result(raise_if_any=False, raise_if_none=False)
            except Exception as exc:
                return f"vision_describe_screen: screenshot failed: {exc}"
            if not shot or not isinstance(shot, str):
                return "vision_describe_screen: empty screenshot"
            data_url = shot if shot.startswith("data:image") else f"data:image/png;base64,{shot}"

            prompt = (
                f"Look at this SCORM e-learning screenshot (HCM Deck, ~1440x920). "
                f"Question: \"{question.strip()}\".\n\n"
                "Answer in ONE concise sentence (max 200 chars). Be specific and "
                "factual; quote any visible text (Polish or English) verbatim. If "
                "the answer is yes/no, say 'YES' or 'NO' first, then a brief reason."
            )
            msg = UserMessage(content=[
                ContentPartTextParam(text=prompt),
                ContentPartImageParam(image_url=ImageURL(url=data_url)),
            ])
            try:
                resp = await llm.ainvoke([msg])
            except Exception as exc:
                return f"vision_describe_screen: llm call failed: {exc}"
            text = ""
            try:
                text = (resp.completion or "").strip()
            except Exception:
                text = str(resp)[:300]
            return f"vision_describe_screen[{question[:40]}]: {text[:300]}"
        except Exception as exc:
            return f"vision_describe_screen failed: {type(exc).__name__}: {exc}"

    @tools.action(
        description=(
            "VISION-FIRST tool. Take a screenshot, ask the vision LLM 'where is X', "
            "get back (x, y) coordinates, then click. DOM-INDEPENDENT — works through "
            "any iframe nesting / cross-origin / SCORM player. USE THIS when scorm_state "
            "shows next_enabled=null but you can SEE the NASTĘPNY/DALEJ button on screen, "
            "or when hotspots exist visually but no DOM elements are clickable. "
            "Argument 'description': brief visual description (e.g. \"the NASTĘPNY button "
            "in the bottom-right corner\", \"the first numbered hotspot circle on the map\", "
            "\"the orange CONTINUE button\")."
        )
    )
    async def vision_locate_and_click(browser_session, description: str) -> str:
        guard = _check_loop("vision_locate_and_click", description[:40])
        if guard:
            return guard
        if llm is None:
            return "vision_locate_and_click: no llm instance bound (registered without llm param)"
        if not description or len(description.strip()) < 4:
            return "vision_locate_and_click: description too short (need >= 4 chars)"
        try:
            from browser_use.llm.messages import (
                UserMessage, ContentPartTextParam, ContentPartImageParam, ImageURL,
            )

            try:
                ev = browser_session.event_bus.dispatch(ScreenshotEvent())
                await ev
                shot = await ev.event_result(raise_if_any=False, raise_if_none=False)
            except Exception as exc:
                return f"vision_locate_and_click: screenshot failed: {exc}"
            if not shot or not isinstance(shot, str):
                return "vision_locate_and_click: empty screenshot"

            top_iframes = await _top_iframes(browser_session)
            iframe_hint = ""
            if top_iframes:
                biggest = max(top_iframes, key=lambda f: (f.get("w") or 0) * (f.get("h") or 0))
                iframe_hint = (
                    f" Note: the visible SCORM area is the iframe at "
                    f"x={biggest.get('x')},y={biggest.get('y')},w={biggest.get('w')},h={biggest.get('h')}. "
                    f"Coordinates must be inside this region for SCORM controls."
                )

            data_url = shot if shot.startswith("data:image") else f"data:image/png;base64,{shot}"

            prompt = (
                f"You are looking at a screenshot of a SCORM e-learning page (HCM Deck, "
                f"viewport ~1440x920). Find this element: \"{description}\".{iframe_hint}\n\n"
                "Return STRICT JSON only, no other text:\n"
                '{"x": INT, "y": INT, "found": true/false, "confidence": 0-100, "reason": "short"}\n\n'
                "x,y are pixel center coordinates in the screenshot. If you cannot see the "
                "element, return found=false."
            )

            msg = UserMessage(content=[
                ContentPartTextParam(text=prompt),
                ContentPartImageParam(image_url=ImageURL(url=data_url)),
            ])
            try:
                resp = await llm.ainvoke([msg])
            except Exception as exc:
                return f"vision_locate_and_click: llm call failed: {exc}"

            text = ""
            try:
                text = (resp.completion or "").strip()
            except Exception:
                text = str(resp)[:500]
            if "```" in text:
                parts = text.split("```")
                for p in parts:
                    if "{" in p and "}" in p:
                        text = p.strip()
                        if text.startswith("json"):
                            text = text[4:].strip()
                        break

            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < 0:
                return f"vision_locate_and_click: no JSON in LLM reply: {text[:200]}"
            try:
                parsed = json.loads(text[start:end + 1])
            except Exception as exc:
                return f"vision_locate_and_click: JSON parse failed: {exc}; raw={text[:200]}"

            if not parsed.get("found"):
                reason = (parsed.get("reason") or "")[:120]
                return f"vision_locate_and_click: element NOT FOUND in screenshot. reason={reason}"
            x = int(parsed.get("x") or 0)
            y = int(parsed.get("y") or 0)
            conf = int(parsed.get("confidence") or 0)
            if x <= 0 or y <= 0:
                return f"vision_locate_and_click: invalid coords ({x},{y}) returned by LLM"
            if conf < 30:
                return f"vision_locate_and_click: low confidence={conf}, NOT clicking. coords=({x},{y})"

            # Fix #1: screenshot-hash diff bypasses nested-iframe blind spots in _take_snapshot.
            import hashlib

            def _shot_hash(s) -> str:
                if not isinstance(s, str) or not s:
                    return ""
                payload = s.split(",", 1)[1] if s.startswith("data:") else s
                return hashlib.sha256(payload.encode("ascii", "ignore")).hexdigest()[:16]

            before_hash = _shot_hash(shot)
            initial = await _take_snapshot(browser_session)
            initial_sig = _signature(initial)
            ok = await _click_coord(browser_session, x, y)
            await asyncio.sleep(1.0)
            shot_after = None
            try:
                ev2 = browser_session.event_bus.dispatch(ScreenshotEvent())
                await ev2
                shot_after = await ev2.event_result(raise_if_any=False, raise_if_none=False)
            except Exception:
                shot_after = None
            after_hash = _shot_hash(shot_after)
            after = await _take_snapshot(browser_session)
            after_sig = _signature(after)
            visual_changed = bool(before_hash and after_hash and before_hash != after_hash)
            sig_changed = initial_sig != after_sig
            changed = visual_changed or sig_changed
            return (
                f"vision_locate_and_click: clicked ({x},{y}) conf={conf} "
                f"reason='{(parsed.get('reason') or '')[:60]}' "
                f"state_changed={changed} (visual={visual_changed} sig={sig_changed}) "
                f"next_enabled={after.get('next_enabled')} dispatch_ok={ok}"
            )[:700]
        except Exception as exc:
            return f"vision_locate_and_click failed: {type(exc).__name__}: {exc}"

    @tools.action(
        description=(
            "Scan the CURRENT page (top + every iframe) for ALTERNATIVE navigation options "
            "WITHOUT leaving the page. Returns compact JSON of: sidebar items, top nav, "
            "breadcrumbs, modules_list (lessons/modules), skip_links ('Pomiń','Następna lekcja'), "
            "pagination, misc_buttons. Each entry has (x,y,text,href,role,cls). "
            "USE THIS when SCORM iframe is stuck instead of reloading or going back to dashboard. "
            "Pick an alternative entry and click_coordinates(x, y)."
        )
    )
    async def page_navigation_options(browser_session) -> str:
        try:
            sessions = await _all_frame_sessions(browser_session)
            iframes = await _top_iframes(browser_session)

            def find_offset_for(label: str) -> tuple[int, int]:
                if label == "top":
                    return (0, 0)
                src = label.split("[", 1)[1].rstrip("]") if "[" in label else ""
                for ifr in iframes:
                    if ifr.get("src") and (src in ifr["src"] or ifr["src"] in src):
                        return (int(ifr.get("x") or 0), int(ifr.get("y") or 0))
                return (0, 0)

            merged: dict = {"top": {}, "frames": []}
            for label, cdp, ctx, _url in sessions:
                data = await _eval_in(cdp, NAV_OPTIONS_JS, context_id=ctx)
                if not isinstance(data, dict):
                    continue
                if label != "top":
                    ox, oy = find_offset_for(label)
                    if ox or oy:
                        for bucket, items in data.items():
                            for it in items:
                                it["x"] = int(it.get("x", 0)) + ox
                                it["y"] = int(it.get("y", 0)) + oy
                if label == "top":
                    merged["top"] = data
                else:
                    merged["frames"].append({"label": label, "options": data})
            return json.dumps(merged, ensure_ascii=False)[:1800]
        except Exception as exc:
            return f"page_navigation_options failed: {exc}"

    # ---- Interactive-stall escape tools -----------------------------------
    # These activate when a SCORM slide has an interactive (canvas/SVG/drag-and-drop/
    # hotspot) that DOM/vision/coord-click can't trigger. They DO NOT participate
    # in the stagnation guard (they ARE the escape). The closure variable
    # `_ESCAPE_TOOLS` above lists them by name.

    @tools.action(
        description=(
            "INTERACTIVE STALL ESCAPE. Brute-force a regular grid of CDP coordinate "
            "clicks across the visible SCORM iframe area. Use when a slide has an "
            "interactive element (hotspot/canvas/SVG region) that DOM tools cannot "
            "find AND vision_locate_and_click misses repeatedly. Default 6x4=24 "
            "click points evenly spaced inside the biggest iframe rectangle. "
            "Arguments: rows (default 6), cols (default 4), settle_ms (default 250)."
        )
    )
    async def scorm_brute_grid_click(
        browser_session,
        rows: int = 6,
        cols: int = 4,
        settle_ms: int = 250,
    ) -> str:
        try:
            rows = max(2, min(int(rows or 6), 10))
            cols = max(2, min(int(cols or 4), 8))
            settle_ms = max(60, min(int(settle_ms or 250), 1000))

            iframes = await _top_iframes(browser_session)
            best = None
            for ifr in iframes:
                if (ifr.get("w") or 0) > 200 and (ifr.get("h") or 0) > 200:
                    if best is None or (ifr["w"] * ifr["h"]) > (best["w"] * best["h"]):
                        best = ifr
            if best is None:
                vw, vh = 1440, 900
                rect = {"x": 80, "y": 80, "w": vw - 160, "h": vh - 160}
            else:
                rect = {
                    "x": int(best["x"]),
                    "y": int(best["y"]),
                    "w": int(best["w"]),
                    "h": int(best["h"]),
                }
            # Inset by 8% so we avoid borders/scrollbars.
            pad_x = max(12, int(rect["w"] * 0.08))
            pad_y = max(12, int(rect["h"] * 0.08))
            inner = (
                rect["x"] + pad_x,
                rect["y"] + pad_y,
                rect["w"] - 2 * pad_x,
                rect["h"] - 2 * pad_y,
            )
            initial = await _take_snapshot(browser_session)
            initial_sig = _signature(initial)
            clicked: list[str] = []
            changes = 0
            for r in range(rows):
                for c in range(cols):
                    x = inner[0] + int(inner[2] * (c + 0.5) / cols)
                    y = inner[1] + int(inner[3] * (r + 0.5) / rows)
                    if await _click_coord(browser_session, x, y):
                        clicked.append(f"({x},{y})")
                        await asyncio.sleep(settle_ms / 1000.0)
                    if len(clicked) % 6 == 0 and clicked:
                        snap = await _take_snapshot(browser_session)
                        sig = _signature(snap)
                        if sig != initial_sig:
                            changes += 1
                            initial_sig = sig
            final = await _take_snapshot(browser_session)
            final_sig = _signature(final)
            return (
                f"scorm_brute_grid_click: clicked {len(clicked)} points in "
                f"{rows}x{cols} grid over iframe rect {rect['w']}x{rect['h']}. "
                f"state_changes={changes} next_enabled={final.get('next_enabled')} "
                f"final_sig_changed={final_sig != _signature(initial)}"
            )[:600]
        except Exception as exc:
            return f"scorm_brute_grid_click failed: {exc}"

    @tools.action(
        description=(
            "INTERACTIVE STALL ESCAPE. Dispatch a keyboard key press via CDP to the "
            "focused frame. Many SCORM authoring tools accept Space/Enter/ArrowRight "
            "to acknowledge a slide read or advance to the next page when the visual "
            "control is hidden/non-interactive. Try keys in this order: 'Space', "
            "'Enter', 'ArrowRight', 'PageDown', 'Tab'. Argument: key (string, one of "
            "Space|Enter|ArrowRight|ArrowDown|ArrowLeft|PageDown|Tab|Escape)."
        )
    )
    async def scorm_keypress(browser_session, key: str = "Space") -> str:
        KEY_MAP = {
            "space":      {"key": " ",          "code": "Space",     "vk": 32, "text": " "},
            "enter":      {"key": "Enter",      "code": "Enter",     "vk": 13, "text": "\r"},
            "arrowright": {"key": "ArrowRight", "code": "ArrowRight","vk": 39, "text": ""},
            "arrowleft":  {"key": "ArrowLeft",  "code": "ArrowLeft", "vk": 37, "text": ""},
            "arrowdown":  {"key": "ArrowDown",  "code": "ArrowDown", "vk": 40, "text": ""},
            "arrowup":    {"key": "ArrowUp",    "code": "ArrowUp",   "vk": 38, "text": ""},
            "pagedown":   {"key": "PageDown",   "code": "PageDown",  "vk": 34, "text": ""},
            "pageup":     {"key": "PageUp",     "code": "PageUp",    "vk": 33, "text": ""},
            "tab":        {"key": "Tab",        "code": "Tab",       "vk":  9, "text": ""},
            "escape":     {"key": "Escape",     "code": "Escape",    "vk": 27, "text": ""},
        }
        try:
            k = (key or "Space").strip().lower()
            spec = KEY_MAP.get(k)
            if not spec:
                return f"scorm_keypress: unknown key '{key}'. Allowed: {sorted(KEY_MAP)}"
            initial = await _take_snapshot(browser_session)
            initial_sig = _signature(initial)
            # Send to every frame's CDP session so cross-origin iframe gets the key too.
            sessions = await _all_frame_sessions(browser_session)
            sent = 0
            for label, cdp, _ctx, _url in sessions:
                try:
                    params_down = {
                        "type": "keyDown",
                        "key": spec["key"],
                        "code": spec["code"],
                        "windowsVirtualKeyCode": spec["vk"],
                    }
                    if spec["text"]:
                        params_down["text"] = spec["text"]
                    await cdp.cdp_client.send.Input.dispatchKeyEvent(
                        params=params_down, session_id=cdp.session_id
                    )
                    if spec["text"]:
                        await cdp.cdp_client.send.Input.dispatchKeyEvent(
                            params={
                                "type": "char",
                                "text": spec["text"],
                                "key": spec["key"],
                                "code": spec["code"],
                                "windowsVirtualKeyCode": spec["vk"],
                            },
                            session_id=cdp.session_id,
                        )
                    await cdp.cdp_client.send.Input.dispatchKeyEvent(
                        params={
                            "type": "keyUp",
                            "key": spec["key"],
                            "code": spec["code"],
                            "windowsVirtualKeyCode": spec["vk"],
                        },
                        session_id=cdp.session_id,
                    )
                    sent += 1
                except Exception:
                    continue
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.6)
            final = await _take_snapshot(browser_session)
            sig_changed = _signature(final) != initial_sig
            return (
                f"scorm_keypress[{spec['key']}]: dispatched to {sent}/{len(sessions)} "
                f"frame contexts. state_changed={sig_changed} "
                f"next_enabled={final.get('next_enabled')}"
            )[:400]
        except Exception as exc:
            return f"scorm_keypress failed: {exc}"

    @tools.action(
        description=(
            "INTERACTIVE STALL ESCAPE. Probe drag-and-drop interactives by "
            "dispatching CDP pointer events (mouseDown -> mouseMove path -> mouseUp). "
            "Tries a few from-to permutations across the SCORM iframe to discover any "
            "valid drag pair. Use when the slide shows draggable cards/labels (e.g. "
            "match-pairs, sortable lists) that hotspot/grid clicks cannot solve. "
            "Argument: max_pairs (default 6)."
        )
    )
    async def scorm_drag_drop_probe(browser_session, max_pairs: int = 6) -> str:
        try:
            max_pairs = max(2, min(int(max_pairs or 6), 12))
            iframes = await _top_iframes(browser_session)
            best = None
            for ifr in iframes:
                if (ifr.get("w") or 0) > 200 and (ifr.get("h") or 0) > 200:
                    if best is None or (ifr["w"] * ifr["h"]) > (best["w"] * best["h"]):
                        best = ifr
            if best is None:
                return "scorm_drag_drop_probe: no iframe rect found, cannot probe"
            x0, y0 = int(best["x"]), int(best["y"])
            w, h = int(best["w"]), int(best["h"])
            # Source points biased to left/center column; targets biased to right column
            # (most match-style interactives have labels on left, drop zones on right).
            sources = [
                (x0 + int(w * 0.20), y0 + int(h * 0.35)),
                (x0 + int(w * 0.20), y0 + int(h * 0.55)),
                (x0 + int(w * 0.20), y0 + int(h * 0.75)),
                (x0 + int(w * 0.45), y0 + int(h * 0.40)),
            ]
            targets = [
                (x0 + int(w * 0.78), y0 + int(h * 0.35)),
                (x0 + int(w * 0.78), y0 + int(h * 0.55)),
                (x0 + int(w * 0.78), y0 + int(h * 0.75)),
                (x0 + int(w * 0.55), y0 + int(h * 0.50)),
            ]
            initial = await _take_snapshot(browser_session)
            initial_sig = _signature(initial)
            pairs_tried = 0
            changes = 0
            cdp_session = await browser_session.get_or_create_cdp_session(focus=False)

            async def _drag(sx, sy, tx, ty) -> bool:
                try:
                    base = {"button": "left", "buttons": 1, "clickCount": 1}
                    await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                        params={"type": "mouseMoved", "x": sx, "y": sy, **base},
                        session_id=cdp_session.session_id,
                    )
                    await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                        params={"type": "mousePressed", "x": sx, "y": sy, **base},
                        session_id=cdp_session.session_id,
                    )
                    # Move along a 5-step path so dragstart fires reliably.
                    for i in range(1, 6):
                        mx = int(sx + (tx - sx) * i / 5)
                        my = int(sy + (ty - sy) * i / 5)
                        await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                            params={"type": "mouseMoved", "x": mx, "y": my, **base},
                            session_id=cdp_session.session_id,
                        )
                        await asyncio.sleep(0.03)
                    await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
                        params={"type": "mouseReleased", "x": tx, "y": ty, **base},
                        session_id=cdp_session.session_id,
                    )
                    return True
                except Exception:
                    return False

            for sx, sy in sources:
                if pairs_tried >= max_pairs:
                    break
                for tx, ty in targets:
                    if pairs_tried >= max_pairs:
                        break
                    pairs_tried += 1
                    ok = await _drag(sx, sy, tx, ty)
                    if not ok:
                        continue
                    await asyncio.sleep(0.5)
                    snap = await _take_snapshot(browser_session)
                    sig = _signature(snap)
                    if sig != initial_sig:
                        changes += 1
                        initial_sig = sig
            final = await _take_snapshot(browser_session)
            return (
                f"scorm_drag_drop_probe: tried {pairs_tried} drag pairs, "
                f"state_changes={changes}, next_enabled={final.get('next_enabled')}"
            )[:400]
        except Exception as exc:
            return f"scorm_drag_drop_probe failed: {exc}"

    # ---- Platform-wide discovery + session memory --------------------------

    # In-memory completed-course registry shared across this agent run.
    # Hydrated from disk so repeated runs (and crash recovery) don't re-process
    # already-finished courses.
    _completed: dict[str, dict] = {}
    try:
        if COMPLETED_PATH.exists():
            raw = json.loads(COMPLETED_PATH.read_text(encoding="utf-8"))
            for entry in (raw.get("items") or []):
                key = entry.get("course_url") or entry.get("course_name") or ""
                if key:
                    _completed[key] = entry
                    if entry.get("course_url"):
                        _completed[entry["course_url"]] = entry
                    if entry.get("course_name"):
                        _completed[entry["course_name"]] = entry
    except Exception:
        pass

    def _flush_completed_to_disk() -> None:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            unique = {id(v): v for v in _completed.values()}
            payload = {"count": len(unique), "items": list(unique.values())}
            COMPLETED_PATH.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    CATALOG_TABS_JS = r"""
    (function () {
      __HELPERS__
      // Polish + English labels for HCM Deck top-level navigation tabs that
      // typically expose distinct sets of courses/tests.
      const KW = [
        "do zrobienia", "do wykonania", "wymagane", "obowiazkowe",
        "w trakcie", "trwajace", "kontynuuj",
        "zakonczone", "ukonczone", "zaliczone", "completed",
        "katalog", "wszystkie szkolenia", "wszystkie kursy", "biblioteka",
        "moje szkolenia", "moja sciezka", "sciezka rozwoju", "sciezki",
        "moodle", "szkolenia", "kursy", "courses", "trainings",
        "to do", "in progress", "library", "catalog", "all courses",
        "home", "pulpit", "dashboard",
        "lekcje", "lessons"
      ];
      function looksTabby(el) {
        const cls = norm(el.className || "");
        const role = (el.getAttribute && el.getAttribute("role")) || "";
        return (
          role === "tab" || role === "menuitem" || role === "link" ||
          cls.includes("tab") || cls.includes("menu") || cls.includes("nav-item") ||
          cls.includes("sidebar") || cls.includes("side-nav") || cls.includes("side-menu") ||
          (el.tagName || "").toLowerCase() === "a"
        );
      }
      const out = [];
      const seen = new Set();
      const candidates = Array.from(document.querySelectorAll(
        "a,button,[role='tab'],[role='menuitem'],[role='link'],li,div,span"
      ));
      for (const el of candidates) {
        if (!isVisible(el)) continue;
        const txt = ((el.innerText || el.textContent || "") + " "
          + (el.getAttribute("aria-label") || "") + " "
          + (el.getAttribute("title") || "")
        ).slice(0, 120);
        const t = norm(txt);
        if (!t || t.length < 3 || t.length > 60) continue;
        if (!KW.some((k) => t.includes(k))) continue;
        if (!looksTabby(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 16 || r.height < 12) continue;
        const href = (el.getAttribute && el.getAttribute("href")) || "";
        const key = t + "|" + Math.round(r.left) + "|" + Math.round(r.top);
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({
          label: (el.innerText || el.textContent || "").trim().slice(0, 80),
          href: href.slice(0, 200),
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          tag: (el.tagName || "").toLowerCase(),
          cls: norm(el.className || "").slice(0, 80)
        });
        if (out.length >= 20) break;
      }
      return { url: location.href, tabs: out };
    })()
    """.replace("__HELPERS__", JS_HELPERS)

    COURSE_CARDS_JS = r"""
    (function () {
      __HELPERS__
      // Course/test/training cards on HCM Deck use varied class names; we
      // heuristically pick any card-like clickable that links into /protected/
      // or contains course/test/training language.
      const out = [];
      const seen = new Set();
      const cardSel = (
        "a[href*='/protected/'], a[href*='course'], a[href*='lesson'], "
        + "a[href*='path'], a[href*='training'], a[href*='test'], "
        + "[class*='card'] a, [class*='tile'] a, [class*='course' i] a, "
        + "[class*='lesson' i] a, [class*='training' i] a, "
        + "[class*='card'][role='button'], [class*='tile'][role='button']"
      );
      let nodes = [];
      try { nodes = Array.from(document.querySelectorAll(cardSel)); } catch (_) {}
      for (const el of nodes) {
        if (!isVisible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 60 || r.height < 30) continue;
        const href = (el.getAttribute && el.getAttribute("href")) || "";
        const text = ((el.innerText || el.textContent || "") + " "
          + (el.getAttribute("aria-label") || "")
        ).replace(/\s+/g, " ").trim().slice(0, 160);
        if (!text && !href) continue;
        const key = (href || "") + "|" + text.slice(0, 60);
        if (seen.has(key)) continue;
        seen.add(key);
        // Try to guess status from nearby badges/labels.
        let status = "";
        try {
          const parent = el.closest("[class*='card'], [class*='tile'], li, article, div") || el.parentElement;
          if (parent) {
            const ptext = norm((parent.innerText || parent.textContent || ""));
            if (ptext.includes("zakonczone") || ptext.includes("ukonczone") || ptext.includes("100%") || ptext.includes("zaliczone")) status = "completed";
            else if (ptext.includes("w trakcie") || ptext.includes("trwajace") || ptext.includes("kontynuuj")) status = "in_progress";
            else if (ptext.includes("do zrobienia") || ptext.includes("rozpocznij") || ptext.includes("start")) status = "todo";
            else if (ptext.includes("wymagane") || ptext.includes("obowiazkowe")) status = "required";
          }
        } catch (_) {}
        out.push({
          name: text.slice(0, 120),
          href: href.slice(0, 240),
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          w: Math.round(r.width),
          h: Math.round(r.height),
          status: status
        });
        if (out.length >= 60) break;
      }
      return { url: location.href, cards: out, count: out.length };
    })()
    """.replace("__HELPERS__", JS_HELPERS)

    PAGINATION_JS = r"""
    (function () {
      __HELPERS__
      const out = { next: null, pages: [] };
      const candidates = Array.from(document.querySelectorAll(
        "a,button,[role='button']"
      ));
      for (const el of candidates) {
        if (!isVisible(el)) continue;
        const t = norm((el.innerText || el.textContent || "")
          + " " + (el.getAttribute("aria-label") || "")
          + " " + (el.getAttribute("title") || ""));
        const cls = norm(el.className || "");
        const r = el.getBoundingClientRect();
        const desc = {
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          text: ((el.innerText || el.textContent || "") + "").trim().slice(0, 30)
        };
        if (cls.includes("pagination") || cls.includes("paginator")) {
          out.pages.push(desc);
        }
        if (!out.next) {
          if (t === ">" || t === "next" || t === "nastepna" || t === "nastepna strona"
              || cls.includes("next") || cls.includes("forward")) {
            const disabled = (el.hasAttribute && el.hasAttribute("disabled"))
              || cls.includes("disabled");
            if (!disabled) out.next = desc;
          }
        }
      }
      out.pages = out.pages.slice(0, 12);
      return out;
    })()
    """.replace("__HELPERS__", JS_HELPERS)

    @tools.action(
        description=(
            "DISCOVERY tool. Scan the dashboard for every top-level section/tab the "
            "user has access to (Polish/English labels: 'Do zrobienia', 'W trakcie', "
            "'Zakończone', 'Katalog', 'Wszystkie szkolenia', 'Moje szkolenia', "
            "'Ścieżki rozwoju', 'Wymagane', 'Library', 'Catalog', etc.). Returns JSON "
            "with each tab's label, href (if anchor), and click coordinates (x,y). "
            "Use this RIGHT AFTER landing on /protected/home to plan section coverage."
        )
    )
    async def enumerate_platform_catalog(browser_session) -> str:
        try:
            top = await browser_session.get_or_create_cdp_session(focus=False)
            data = await _eval_in(top, CATALOG_TABS_JS)
            if not isinstance(data, dict):
                return "enumerate_platform_catalog: no payload (page may still be loading; retry after a screenshot)"
            tabs = data.get("tabs") or []
            return json.dumps({"url": data.get("url"), "tabs_count": len(tabs), "tabs": tabs}, ensure_ascii=False)[:1800]
        except Exception as exc:
            return f"enumerate_platform_catalog failed: {exc}"

    @tools.action(
        description=(
            "DISCOVERY tool. List every course/test/training tile (card) currently "
            "visible on the page across the top document and accessible iframes. "
            "Returns JSON with each card's name, href, click coordinates (x,y) and "
            "guessed status ('completed' | 'in_progress' | 'todo' | 'required' | ''). "
            "Also surfaces pagination 'next' coordinates so you can page through long "
            "catalog lists. Call this AFTER clicking a section tab so you get the "
            "full course list for that section."
        )
    )
    async def gather_course_cards(browser_session) -> str:
        try:
            sessions = await _all_frame_sessions(browser_session)
            iframes = await _top_iframes(browser_session)

            def find_offset_for(label: str) -> tuple[int, int]:
                if label == "top":
                    return (0, 0)
                src = label.split("[", 1)[1].rstrip("]") if "[" in label else ""
                for ifr in iframes:
                    if ifr.get("src") and (src in ifr["src"] or ifr["src"] in src):
                        return (int(ifr.get("x") or 0), int(ifr.get("y") or 0))
                return (0, 0)

            all_cards: list[dict] = []
            seen_keys: set[str] = set()
            for label, cdp, ctx, _url in sessions:
                data = await _eval_in(cdp, COURSE_CARDS_JS, context_id=ctx)
                if not isinstance(data, dict):
                    continue
                ox, oy = find_offset_for(label)
                for c in (data.get("cards") or []):
                    key = (c.get("href") or "") + "|" + (c.get("name") or "")[:60]
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    if ox or oy:
                        c["x"] = int(c.get("x", 0)) + ox
                        c["y"] = int(c.get("y", 0)) + oy
                    all_cards.append(c)
                if len(all_cards) >= 60:
                    break

            top = await browser_session.get_or_create_cdp_session(focus=False)
            pag = await _eval_in(top, PAGINATION_JS)
            if not isinstance(pag, dict):
                pag = {"next": None, "pages": []}

            completed_keys = set(_completed.keys())
            for c in all_cards:
                href = (c.get("href") or "").strip()
                name = (c.get("name") or "").strip()
                if href in completed_keys or name in completed_keys:
                    c["already_done_in_run"] = True

            payload = {
                "url": (sessions[0][3] if sessions else ""),
                "cards_count": len(all_cards),
                "cards": all_cards[:50],
                "pagination": pag,
                "already_done_in_run": sum(1 for c in all_cards if c.get("already_done_in_run")),
            }
            return json.dumps(payload, ensure_ascii=False)[:1800]
        except Exception as exc:
            return f"gather_course_cards failed: {exc}"

    VERIFY_BADGE_JS = r"""
    (function (needle) {
      __HELPERS__
      const KW_DONE = [
        "ukonczone", "zakonczone", "uzyskano", "zaliczone", "completed",
        "passed", "finished", "done", "100%", "100 %"
      ];
      const KW_PARTIAL = [
        "w trakcie", "in progress", "kontynuuj", "continue", "rozpocznij",
        "start", "do zrobienia", "to do", "wymagane", "obowiazkowe",
        "pending", "incomplete"
      ];
      const needleN = norm(needle || "");
      let foundCardText = "";
      let progressPct = null;
      let badgeStatus = "";

      // Look for any element whose text contains the course-name needle. From
      // that element walk up to its card container and scan for progress %.
      const all = Array.from(document.querySelectorAll("*"));
      for (const el of all) {
        if (!isVisible(el)) continue;
        const txt = (el.innerText || el.textContent || "").trim();
        if (!txt || txt.length > 500) continue;
        const tn = norm(txt);
        if (!needleN) continue;
        if (!tn.includes(needleN.slice(0, Math.min(28, needleN.length)))) continue;
        // Walk up to a card-like container (max 6 hops).
        let card = el;
        for (let i = 0; i < 6 && card && card.parentElement; i++) {
          const cls = norm(card.className || "");
          if (cls.includes("card") || cls.includes("tile") ||
              cls.includes("course") || cls.includes("training") ||
              card.tagName === "ARTICLE" || card.tagName === "LI") {
            break;
          }
          card = card.parentElement;
        }
        if (!card) continue;
        const ctxText = (card.innerText || card.textContent || "").trim();
        const ctxN = norm(ctxText);
        foundCardText = ctxText.slice(0, 240);
        // Parse a percentage (e.g. "75%", "100 %", "50 / 100").
        const pctMatch = ctxText.match(/\b(\d{1,3})\s?%/);
        if (pctMatch) progressPct = parseInt(pctMatch[1], 10);
        const fracMatch = ctxText.match(/\b(\d{1,3})\s?\/\s?(\d{1,3})\b/);
        if (progressPct == null && fracMatch) {
          const a = parseInt(fracMatch[1], 10);
          const b = parseInt(fracMatch[2], 10);
          if (b > 0) progressPct = Math.round((a / b) * 100);
        }
        // Look for status keyword.
        for (const k of KW_DONE) {
          if (ctxN.includes(k)) { badgeStatus = "completed"; break; }
        }
        if (!badgeStatus) {
          for (const k of KW_PARTIAL) {
            if (ctxN.includes(k)) { badgeStatus = "partial"; break; }
          }
        }
        // Also look for any progress-bar-like element with aria-valuenow.
        try {
          const pb = card.querySelector("[role='progressbar'],[aria-valuenow]");
          if (pb) {
            const v = parseInt(pb.getAttribute("aria-valuenow") || "0", 10);
            const max = parseInt(pb.getAttribute("aria-valuemax") || "100", 10) || 100;
            if (!isNaN(v) && progressPct == null) {
              progressPct = Math.round((v / max) * 100);
            }
          }
        } catch (_) {}
        if (foundCardText) break;
      }
      return {
        found: !!foundCardText,
        cardText: foundCardText,
        progressPct: progressPct,
        badgeStatus: badgeStatus
      };
    })(__NEEDLE__)
    """.replace("__HELPERS__", JS_HELPERS)

    @tools.action(
        description=(
            "COMPLETION VERIFIER. Inspect the dashboard / course catalog / details "
            "modal for the REAL completion badge of a course. Returns JSON with "
            "found, cardText, progressPct (0-100 or null), badgeStatus ('completed' "
            "| 'partial' | ''). USE THIS BEFORE every mark_course_done(status='SUCCESS') "
            "— mark_course_done now REJECTS SUCCESS unless you pass verified=True, "
            "and you can only honestly do that after this tool confirms either "
            "progressPct >= 100 OR badgeStatus='completed'. "
            "Argument: course_name (string) — the visible course title to look for. "
            "Workflow: navigate back to course catalog tab (or open details modal), "
            "then call verify_course_completion('Counteracting Mobbing'). If "
            "progressPct < 100 (e.g. 50/75%), the course has MORE modules to do — "
            "DO NOT mark SUCCESS, instead return to the course and complete remaining "
            "modules via the TOC/sidebar."
        )
    )
    async def verify_course_completion(
        browser_session, course_name: str = ""
    ) -> str:
        try:
            if not course_name or len(course_name.strip()) < 3:
                return "verify_course_completion: provide a course_name (>=3 chars)"
            needle = course_name.strip()
            sessions = await _all_frame_sessions(browser_session)
            expression = VERIFY_BADGE_JS.replace("__NEEDLE__", json.dumps(needle))
            best = None
            for label, cdp, ctx, _url in sessions:
                data = await _eval_in(cdp, expression, context_id=ctx)
                if not isinstance(data, dict) or not data.get("found"):
                    continue
                if best is None:
                    best = {**data, "frame": label}
                # Prefer entries that actually got a progressPct value.
                elif data.get("progressPct") is not None and best.get("progressPct") is None:
                    best = {**data, "frame": label}
            if best is None:
                return (
                    "verify_course_completion: course card NOT FOUND in current view. "
                    "Navigate to the course catalog / dashboard FIRST (so the card is "
                    "visible), then retry. DO NOT mark_course_done(SUCCESS) — you have "
                    "no verification of real completion."
                )
            pct = best.get("progressPct")
            badge = best.get("badgeStatus") or ""
            is_done = (pct is not None and pct >= 100) or badge == "completed"
            verdict = "VERIFIED_COMPLETE" if is_done else "NOT_COMPLETE"
            return (
                f"verify_course_completion[{verdict}]: progressPct={pct} "
                f"badgeStatus='{badge}' frame={best.get('frame','')[:30]} "
                f"cardText='{(best.get('cardText') or '')[:160]}'. "
                + (
                    "OK to mark_course_done(status='SUCCESS', verified=True)."
                    if is_done
                    else "DO NOT mark SUCCESS — return to course and finish "
                         "remaining modules via TOC/sidebar; pct < 100 means there "
                         "is more content."
                )
            )[:700]
        except Exception as exc:
            return f"verify_course_completion failed: {exc}"

    @tools.action(
        description=(
            "SESSION MEMORY. Record that a course/test has been finished in this run "
            "so you don't re-enter it. Arguments: course_url (string), course_name "
            "(string), score_percent (float 0-100), status (e.g. 'SUCCESS', "
            "'PARTIAL_SUCCESS', 'PLATFORM_LIMIT', 'ALREADY_DONE'), "
            "verified (bool, REQUIRED for SUCCESS — must be True). "
            "ANTI-LIE GUARD: status='SUCCESS' is REJECTED unless verified=True. "
            "To pass verified=True you must FIRST call verify_course_completion(...) "
            "and confirm the dashboard progress badge shows 100% (or status='Completed' / "
            "'Ukończone' / 'Zakończone'). If the SCORM player shows 'Bye!' / 'End of "
            "module' but the dashboard progress bar is < 100%, the course is NOT done — "
            "there are more modules/sections to do. Multi-module courses (Inclusive "
            "Language=3, Rules of feedback=5 sections, Working remotely=multi-module) "
            "have caused FALSE SUCCESS reports in the past — always verify."
        )
    )
    async def mark_course_done(
        browser_session,
        course_url: str = "",
        course_name: str = "",
        score_percent: float = 0.0,
        status: str = "SUCCESS",
        verified: bool = False,
    ) -> str:
        try:
            key = (course_url or course_name or "").strip()
            if not key:
                return "mark_course_done: empty key (provide course_url or course_name)"
            status_norm = (status or "SUCCESS").strip().upper()
            # ANTI-LIE GUARD: SUCCESS / ALREADY_DONE without verified=True is rejected.
            # PLATFORM_LIMIT / PARTIAL_SUCCESS / FAILED don't need verification — they
            # are explicit declarations that the course did NOT complete.
            if status_norm in ("SUCCESS", "ALREADY_DONE") and not verified:
                return (
                    f"mark_course_done REJECTED: status='{status_norm}' requires "
                    f"verified=True. First call verify_course_completion(course_url or "
                    f"course_name) to check the REAL dashboard progress badge — if it "
                    f"shows 100% / Completed / Ukończone, retry mark_course_done with "
                    f"verified=True. Multi-module SCORM courses often show 'Bye!' / "
                    f"'End of module' on a single module while the OVERALL course is "
                    f"still partial. DO NOT mark SUCCESS based only on a SCORM end "
                    f"screen — always cross-check the catalog/dashboard badge."
                )
            entry = {
                "course_url": (course_url or "").strip()[:300],
                "course_name": (course_name or "").strip()[:160],
                "score_percent": float(score_percent or 0.0),
                "status": status_norm[:40],
                "verified": bool(verified),
            }
            _completed[key] = entry
            if course_url and course_url != key:
                _completed[course_url] = entry
            if course_name and course_name != key:
                _completed[course_name] = entry
            _flush_completed_to_disk()
            return (
                f"mark_course_done: recorded '{entry['course_name'][:60]}' "
                f"status={entry['status']} score={entry['score_percent']:.1f}. "
                f"Total done={len({id(v) for v in _completed.values()})}. "
                f"(checkpoint -> {COMPLETED_PATH})"
            )
        except Exception as exc:
            return f"mark_course_done failed: {exc}"

    @tools.action(
        description=(
            "SESSION MEMORY. Return the list of courses/tests already finished in "
            "this run (recorded via mark_course_done). Use BEFORE opening a course "
            "from gather_course_cards so you skip duplicates."
        )
    )
    async def get_completed_courses(browser_session) -> str:
        try:
            unique = {id(v): v for v in _completed.values()}
            items = list(unique.values())
            return json.dumps({"count": len(items), "items": items}, ensure_ascii=False)[:1800]
        except Exception as exc:
            return f"get_completed_courses failed: {exc}"

    return tools


# --- LLM ---------------------------------------------------------------------


def create_llm(*, profile: str = "primary"):
    """Create an Azure OpenAI ChatOpenAI client for browser-use.

    For best results, deploy a direct multimodal model in Azure OpenAI Studio
    (gpt-5-chat, gpt-4o, or model-router-2025-11-18 or newer) and point
    AZURE_OPENAI_DEPLOYMENT at it. See the README for step-by-step deployment
    instructions.

    Two sampling profiles support browser-use's primary + fallback retry chain:
        profile='primary'  — main LLM: reasoning_effort=low, temperature=0.1,
                             max_retries=6. When AZURE_OPENAI_DEPLOYMENT is a
                             model-router, vision content reliably routes to a
                             multimodal underlying model.
        profile='fallback' — retry LLM used when primary raises ModelProviderError
                             (e.g. empty JSON completion). Effort=minimal,
                             temperature=0.0 — deterministic, less likely to
                             return empty for plain JSON-mode requests.
    """
    from browser_use import ChatOpenAI

    missing = [n for n, v in (
        ("AZURE_OPENAI_API_KEY", AZURE_OPENAI_API_KEY),
        ("AZURE_OPENAI_ENDPOINT", AZURE_OPENAI_ENDPOINT),
        ("AZURE_OPENAI_DEPLOYMENT", AZURE_OPENAI_DEPLOYMENT),
    ) if not v]
    if missing:
        raise ValueError(
            "Missing Azure config in .env: " + ", ".join(missing)
            + ". Fill them before running the agent."
        )

    endpoint = AZURE_OPENAI_ENDPOINT.rstrip("/")
    base_url = f"{endpoint}/openai/deployments/{AZURE_OPENAI_DEPLOYMENT}"

    if profile == "fallback":
        # Minimal-effort deterministic profile. Use when primary returns
        # empty/invalid JSON — model-router routes minimal+temp0 most often to
        # gpt-5-mini which never returns empty for plain JSON-mode requests.
        return ChatOpenAI(
            model=AZURE_OPENAI_DEPLOYMENT,
            api_key=AZURE_OPENAI_API_KEY,
            base_url=base_url,
            reasoning_effort="minimal",
            temperature=0.0,
            max_retries=4,
            default_query={"api-version": AZURE_OPENAI_API_VERSION},
        )

    # Primary profile — vision-biased, slightly creative, more retries.
    # reasoning_effort="low" + vision content forces model-router-2 to pick
    # the multimodal route (gpt-5-chat-2025-08-07) rather than gpt-5-mini.
    return ChatOpenAI(
        model=AZURE_OPENAI_DEPLOYMENT,
        api_key=AZURE_OPENAI_API_KEY,
        base_url=base_url,
        reasoning_effort="low",
        temperature=0.1,
        max_retries=6,
        default_query={"api-version": AZURE_OPENAI_API_VERSION},
    )


def build_tools(llm=None):
    """Build the Tools instance with exclusions + custom SCORM tools.

    llm (optional): if provided, vision_locate_and_click can call it for
    screenshot-based element location. Without llm, that tool returns an error.
    """
    from browser_use import Tools

    tools = Tools(exclude_actions=list(EXCLUDED_ACTIONS))
    tools.set_coordinate_clicking(True)
    register_custom_tools(tools, llm=llm)
    return tools


# --- CLI ---------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HCM Deck course agent (OOPIF rewrite).")
    parser.add_argument(
        "--url",
        help=f"Course or dashboard URL (default: {DEFAULT_DASHBOARD_URL}).",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=3000,
        help=(
            "Step budget (default 3000 — enough headroom for full-platform sweep "
            "covering every section: Do zrobienia, W trakcie, Zakończone, Katalog)."
        ),
    )
    parser.add_argument("--target-score", type=int, default=81, help="Target score (default 81).")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Verbose debug mode: BROWSER_USE_LOGGING_LEVEL=debug, prints every "
            "action input/output, tees full run log to data/agent_run_<ts>.log."
        ),
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        help="Clear data/completed_courses.json before running (fresh sweep).",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Validate tools/prompt/schema without launching a browser or LLM call.",
    )
    return parser.parse_args()


# --- Smoke test --------------------------------------------------------------


def smoke_test(args: argparse.Namespace) -> int:
    print("=" * 60)
    print("SMOKE TEST")
    print("=" * 60)

    print("[1/5] Imports...")
    from browser_use import Agent, Browser, Tools  # noqa: F401
    from browser_use.browser.events import ClickCoordinateEvent  # noqa: F401
    print("      OK")

    print("[2/5] Build Tools + custom actions...")
    tools = build_tools()
    actions = tools.registry.registry.actions
    expected_custom = {
        "scorm_state",
        "scorm_force",
        "scorm_explore_hotspots",
        "scorm_dismiss_overlay",
        "scorm_wait_for_ready",
        "scorm_brute_grid_click",
        "scorm_keypress",
        "scorm_drag_drop_probe",
        "page_navigation_options",
        "vision_locate_and_click",
        "vision_describe_screen",
        "enumerate_platform_catalog",
        "gather_course_cards",
        "mark_course_done",
        "get_completed_courses",
        "verify_course_completion",
    }
    missing = [name for name in expected_custom if name not in actions]
    if missing:
        print(f"      FAIL: missing custom actions: {missing}")
        return 1
    print(f"      OK ({len(actions)} actions registered, custom={sorted(expected_custom)})")

    print("[3/5] Exclusion sanity check...")
    leaked = [a for a in EXCLUDED_ACTIONS if a in actions]
    if leaked:
        print(f"      FAIL: excluded actions still present: {leaked}")
        return 1
    print(f"      OK ({len(EXCLUDED_ACTIONS)} actions excluded as expected)")

    print("[4/5] Schema round-trip...")
    sample = CourseRunSummary(
        entry_url="https://example.test/dashboard",
        status="PARTIAL_SUCCESS",
        courses_total=2,
        courses_completed=1,
        average_score_percent=50.0,
        target_score_percent=args.target_score,
        blocking_reason="",
        processed_courses=[
            CourseResult(
                course_name="Demo",
                course_url="https://example.test/c/1",
                status="SUCCESS",
                score_percent=100.0,
                notes="ok",
            )
        ],
        actions_taken=["opened dashboard", "completed Demo"],
        next_best_action="run again to finish remaining course",
    )
    payload = json.dumps(sample.model_dump(), ensure_ascii=False)
    rt = CourseRunSummary.model_validate_json(payload)
    if rt.status != "PARTIAL_SUCCESS" or len(rt.processed_courses) != 1:
        print("      FAIL: schema round-trip mismatch")
        return 1
    print("      OK")

    print("[5/5] Prompt sanity check...")
    prompt = build_task("https://example.test/dashboard", args.target_score)
    must_contain = [
        "HARD RULES", "PLAYBOOK",
        "scorm_state", "scorm_force",
        "scorm_explore_hotspots", "scorm_dismiss_overlay",
        "scorm_wait_for_ready",
        "scorm_brute_grid_click", "scorm_keypress", "scorm_drag_drop_probe",
        "page_navigation_options",
        "vision_locate_and_click",
        "vision_describe_screen",
        "enumerate_platform_catalog",
        "gather_course_cards",
        "mark_course_done",
        "get_completed_courses",
        "verify_course_completion",
        "NEVER reload", "SSO RECOVERY ALLOWED",
        "QUIZ / TEST HANDLING",
        "PLATFORM-WIDE COVERAGE PLAYBOOK",
        "INTERACTIVE STALL ESCAPE PLAYBOOK",
        "COMPLETION VERIFICATION PROTOCOL",
        "HARD_ANTI_LOOP_STAGNATION",
        "Do zrobienia",
        "Katalog",
        "STOP CONDITION",
    ]
    missing_in_prompt = [m for m in must_contain if m not in prompt]
    if missing_in_prompt:
        print(f"      FAIL: prompt missing fragments: {missing_in_prompt}")
        return 1
    print(f"      OK (prompt length: {len(prompt)} chars)")

    print()
    print("Smoke test passed. Agent wiring looks healthy.")
    return 0


# --- Run ---------------------------------------------------------------------


async def run_agent(args: argparse.Namespace) -> CourseRunSummary:
    from browser_use import Agent, Browser

    if not Path(CHROME_EXE).exists():
        raise SystemExit(f"Chrome executable not found: {CHROME_EXE}")
    if not Path(USER_DATA).exists():
        raise SystemExit(f"Chrome user data folder not found: {USER_DATA}")

    llm = create_llm()
    tools = build_tools(llm=llm)

    browser = Browser(
        executable_path=CHROME_EXE,
        user_data_dir=USER_DATA,
        headless=args.headless,
        window_size={"width": 1600, "height": 1000},
        # Wymuszone widoczne okno na foreground (zeby user mogl podgladac).
        args=[
            "--start-maximized",
            "--window-position=0,0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=GlobalMediaControls",
        ],
    )

    # Fallback LLM picks up when primary returns ModelProviderError
    # (typically Azure model-router returning empty completion for vision+JSON).
    # 'minimal' effort + temp 0 routes more reliably to gpt-5-mini which is
    # deterministic enough to break the JSON-EOF loop.
    fallback_llm = create_llm(profile="fallback")

    agent = Agent(
        task=build_task(args.url, args.target_score),
        llm=llm,
        fallback_llm=fallback_llm,
        browser=browser,
        tools=tools,
        output_model_schema=CourseRunSummary,
        use_vision=True,
        use_judge=False,
        # enable_planning=True — agent najpierw tworzy mini-plan strategii (np. dla
        # quizu: read question -> enumerate options -> try option 1 -> check feedback)
        # przed kazdym step. Lepsze decyzje, mniej powtarzania bledow.
        enable_planning=True,
        # max_actions_per_step=3 — agent moze wykonac do 3 akcji w jednym kroku LLM
        # (np. scorm_state + vision_describe_screen + vision_locate_and_click w jednym
        # kroku zamiast 3 osobnych). 50% mniej tokenow conversation history per zadanie.
        max_actions_per_step=3,
        # 3000-step run sees many transient SCORM/LLM hiccups (network blips,
        # cross-origin iframe reload, vision LLM occasional timeouts). Higher
        # tolerance keeps the run alive instead of bailing after a small streak.
        max_failures=30,
        llm_timeout=240,
    )

    fallback = CourseRunSummary(
        entry_url=args.url,
        status="FAILED",
        courses_total=0,
        courses_completed=0,
        average_score_percent=0.0,
        target_score_percent=args.target_score,
        blocking_reason="",
        processed_courses=[],
        actions_taken=[],
        next_best_action="",
    )
    result = fallback

    try:
        history = await agent.run(max_steps=args.max_steps)
        try:
            parsed = history.structured_output
        except Exception as exc:
            parsed = None
            fallback.blocking_reason = (f"Structured output parse error: {exc}")[:300]
        if parsed is not None:
            result = parsed
        else:
            final_text = history.final_result() if history else ""
            fallback.next_best_action = (final_text or "")[:300]
            fallback.actions_taken = ["Agent run ended without structured output."]
            result = fallback
    except Exception as exc:
        fallback.blocking_reason = (f"Runtime exception: {exc}")[:300]
        fallback.actions_taken = ["Run interrupted by an exception."]
        result = fallback
    finally:
        try:
            await browser.stop()
        except Exception:
            pass

    return result


def print_summary(result: CourseRunSummary) -> None:
    print("\n" + "=" * 60)
    print("COURSE RUN RESULT")
    print("=" * 60)
    print(f"Status:               {result.status}")
    print(f"Courses detected:     {result.courses_total}")
    print(f"Courses completed:    {result.courses_completed}")
    print(f"Average score:        {result.average_score_percent:.2f}%")
    print(f"Target:               {result.target_score_percent}%")
    if result.blocking_reason:
        print(f"Blocking reason:      {result.blocking_reason}")
    if result.actions_taken:
        print("\nActions taken:")
        for a in result.actions_taken:
            print(f"- {a}")
    if result.processed_courses:
        print("\nProcessed courses:")
        for i, c in enumerate(result.processed_courses, 1):
            print(f"{i}. {c.course_name} | {c.status} | {c.score_percent:.2f}% | {c.notes}")
    if result.next_best_action:
        print(f"\nNext best action: {result.next_best_action}")


async def main_async(args: argparse.Namespace) -> None:
    if not args.url:
        if DEFAULT_DASHBOARD_URL:
            args.url = DEFAULT_DASHBOARD_URL
            print(f"[info] --url not provided; using HCM_DASHBOARD_URL env: {args.url}")
        else:
            raise SystemExit(
                "ERROR: no dashboard URL configured.\n"
                "  Either pass --url <https://your-tenant.hcmdeck.com/protected/home>\n"
                "  or set HCM_DASHBOARD_URL in .env to your HCM Deck tenant home page."
            )
    if args.reset_checkpoint and COMPLETED_PATH.exists():
        try:
            COMPLETED_PATH.unlink()
            print(f"[info] --reset-checkpoint: removed {COMPLETED_PATH}")
        except Exception as exc:
            print(f"[warn] --reset-checkpoint failed: {exc}")
    result = await run_agent(args)
    DATA_DIR.mkdir(exist_ok=True)
    RESULT_PATH.write_text(
        json.dumps(result.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print_summary(result)
    print(f"\nSaved structured summary to: {RESULT_PATH}")


def _setup_debug_logging() -> Path:
    """Enable verbose browser-use logs and tee everything to a timestamped file."""
    import logging
    from datetime import datetime

    DATA_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DATA_DIR / f"agent_run_{ts}.log"

    # browser-use reads BROWSER_USE_LOGGING_LEVEL at import-time of its logging
    # bootstrap; setting it here only takes effect for logging.* configured below.
    os.environ["BROWSER_USE_LOGGING_LEVEL"] = "debug"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler — INFO-and-above on stdout to keep terminal readable while
    # the file gets the full DEBUG firehose. (browser_use logger already prints
    # to stdout by default; this just makes the format consistent.)
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    for noisy in ("urllib3", "httpcore", "httpx.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    print(f"[debug] Verbose log file: {log_path}")
    return log_path


def main() -> int:
    args = parse_args()
    if args.smoke_test:
        return smoke_test(args)
    if args.debug:
        _setup_debug_logging()
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

