# HCM Deck Course Agent

> **Your company told you to finish 7 mandatory compliance trainings by Friday?**
> 90 minutes each. SCORM, quizzes, NPS feedback, "click every hotspot to advance".
> ~10 hours of clicking total. This agent does it for you in **3 hours in the
> background** — you get on with real work, by evening every course is ✅ at 100%.

An autonomous browser agent for **HCM Deck** (a popular LMS in Central/Eastern
Europe used by many large enterprises for mandatory compliance training). It
signs in through your corporate SSO, finds **every** course/test/training on
the platform, walks through them page by page, solves quizzes (with retry on
wrong answers), submits the NPS feedback, and verifies that the dashboard
actually shows 100% — not just that the SCORM player printed "Bye!".

Built on top of [browser-use](https://github.com/browser-use/browser-use)
(open-source). Runs inside **your** Chrome with **your** signed-in sessions —
never types passwords, doesn't exfiltrate cookies, doesn't ship anything
anywhere except your chosen LLM provider.

---

## Why this exists (real-run context)

A typical corporate onboarding / compliance pack looks like this:

| Course | Manual time | What's inside |
|---|---|---|
| Phishing Awareness | 30–45 min | slides + 10-question test |
| Anti-Corruption Policy | 60 min | multi-module, hotspots, quiz |
| Mobbing & Discrimination | 90 min | 3 sections × 22 pages + quiz + NPS |
| Working remotely | 60 min | interactive hotspots, quiz |
| Inclusive Language | 75 min | 3 modules × ~15 slides |
| Rules of feedback | 60 min | 5 sections + accordions + video + quiz |
| Mandatory country path | 90 min | a chained path of 3 courses |
| **TOTAL** | **~7.5–10 h** | ~600 clicks, ~50 quiz answers |

**An agent run over that pack**: 56 LLM steps, 0 errors, **100% completed,
~3h wall-clock** (during which the computer works, you do something else).
Real data from the latest run — `data/course_run_summary.json`.

### What this agent does that other "browser bots" don't

- **Vision-first navigation through cross-origin SCORM iframes** (per-frame
  OOPIF CDP attach) — HCM Deck SCORMs live inside a nested iframe on a
  different origin; classic DOM tools are blind. The agent screenshots the
  viewport, asks the vision LLM "where is the NEXT button", clicks via CDP
  coordinate click.
- **Interactive Stall Escape Protocol** — when a slide has a canvas/SVG/
  drag-and-drop interactive that neither DOM nor vision can move (e.g.
  "click each of 4 points on the map"), the agent fires a cascade: brute-grid
  (24 click points 6×4) → keypress (Space/Enter/ArrowRight) → drag-drop probe
  → page-navigation fallback. This rescued the Mobbing course, which an
  earlier run had been stuck on at Page 10/22.
- **Anti-lie completion verification** — `verify_course_completion()` is
  called before every SUCCESS save; it returns to the dashboard and reads
  the REAL progress badge (%, "Completed", `aria-valuenow` on the progress
  bar). `mark_course_done(SUCCESS)` is hard-rejected without `verified=True`.
  This guard came from a real bug: the agent saw "End of module 1" in a
  three-module course, marked it as 100% — the dashboard was showing 33%.
  Now that's impossible.
- **Signature-based anti-loop** — the classic "same tool with same args ≥4
  times" plus a second guard: "≥8 calls of ANY tools without a change in
  snapshot signature ⇒ HARD_ANTI_LOOP_STAGNATION + force the escape protocol".
- **Durable checkpoint** — every finished course is written to
  `data/completed_courses.json` immediately on `mark_course_done`. A crash
  2 hours into a run doesn't lose the work — a re-run reads the checkpoint
  and skips what's already done.
- **Structured output** (Pydantic) — a ready-to-consume JSON report with
  per-course scoring, status, blockers and a list of actions taken. Pipe it
  straight into ServiceNow / Jira / a reporting dashboard with no text
  parsing.
- **3000-step budget + fallback LLM** — long courses don't derail the run.
  A second LLM profile (minimal effort, temp 0) kicks in when Azure's
  model-router returns an empty JSON.

---

## 60-second architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Your Chrome (your profile, your SSO cookies)               │
│  ┌─ HCM Deck dashboard ─┐  ┌─ SCORM popup ────────────────┐ │
│  │ To do      (7)       │  │  runLessonContainer iframe   │ │
│  │ In progress (3)      │  │   └─ lessonFrame (OOPIF)     │ │
│  │ Catalog    (12)      │  │       └─ your SCORM content  │ │
│  └─────────▲────────────┘  └──────────────▲───────────────┘ │
└────────────┼───────────────────────────────┼─────────────────┘
             │ CDP (Chrome DevTools Protocol) per-frame
┌────────────┴───────────────────────────────┴─────────────────┐
│  browser-use Agent  +  course_agent.py custom tools (16)     │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Discovery: enumerate_platform_catalog,                 │  │
│  │            gather_course_cards, page_navigation_options│  │
│  │ SCORM:     scorm_state, scorm_force, scorm_explore,    │  │
│  │            scorm_dismiss_overlay, scorm_wait_for_ready │  │
│  │ Stall:     scorm_brute_grid_click, scorm_keypress,     │  │
│  │            scorm_drag_drop_probe                       │  │
│  │ Vision:    vision_locate_and_click, vision_describe    │  │
│  │ Memory:    mark_course_done(verified=True),            │  │
│  │            verify_course_completion, get_completed     │  │
│  └────────────────────────────────────────────────────────┘  │
└────────────────────────▲─────────────────────────────────────┘
                         │ Chat Completions API
                  ┌──────┴──────────────┐
                  │ Azure OpenAI        │  ← RECOMMENDED
                  │   gpt-5-chat / 4o   │
                  │  or Anthropic /     │
                  │  OpenAI / Gemini    │
                  └─────────────────────┘
```

---

## Install — 5 steps, 10 minutes

### Prerequisites

- **Windows 10/11** (Linux/macOS will work after small Chrome-path tweaks
  in `agents/course_agent.py:53-57`)
- **Python 3.11 or 3.12** — grab it from
  [python.org](https://www.python.org/downloads/) or
  `winget install Python.Python.3.12`. Tick "Add to PATH".
- **Google Chrome** installed with your corporate SSO already signed in
- **An LLM API key** (Azure OpenAI / Anthropic / OpenAI / Google — see below)

### Step 1 — clone and enter the folder

```powershell
git clone https://github.com/<YOUR-USER>/hcm-deck-agent.git
cd hcm-deck-agent
```

### Step 2 — unblock PowerShell (one-time)

In PowerShell **as administrator**:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

If that's a problem — use `install.bat` instead of `install.ps1`.

### Step 3 — run the installer

```powershell
.\install.ps1
```

The installer: verify Python → install `uv` → create `.venv` → install
dependencies → download Chromium (~130 MB; skippable if you already have a
Chrome profile you want to reuse) → create `.env` from the template.

### Step 4 — fill in `.env`

```powershell
notepad .env
```

Three things are required:

1. `HCM_DASHBOARD_URL` — e.g. `https://acme.hcmdeck.com/protected/home`
2. **LLM keys** — see [LLM configuration](#llm-configuration--what-we-recommend)
3. `CHROME_EXE` / `CHROME_USER_DATA` — only if Chrome lives somewhere
   non-default (the defaults match a stock Windows install)

### Step 5 — run the agent

```powershell
.\.venv\Scripts\activate
python agents\course_agent.py --smoke-test       # validate the wiring
python agents\course_agent.py --debug            # full run, 3000 steps
```

Recommended first run: kick it off as you go to lunch / a meeting / sleep.
The agent works in the background, you can watch progress in
`data/agent_run_<ts>.log`.

---

## LLM configuration — what we recommend

What we used while building this and what we recommend:

### 🥇 Azure OpenAI / Foundry — RECOMMENDED for companies

Best quality-to-cost ratio if your organisation already has an Azure OpenAI
resource. Importantly — when paired with your corporate SSO, no data
leaves your tenant.

**Best deployments (in order of preference)**:

| Model | Type | Why | Notes |
|---|---|---|---|
| **`gpt-5-chat-2025-10-03`** | multimodal flagship preview | Best for vision-heavy UI nav | Needs a deployment, ~$2-5/run |
| **`gpt-5-chat-2025-08-07`** | multimodal preview (older) | Proven, stable | Needs a deployment |
| **`gpt-4o-2024-11-20`** | proven multimodal | Reliable baseline, GA | Needs a deployment |
| **`model-router-2025-11-18`** | router GA (newest) | Auto-routes to gpt-5-chat for vision | Simplest: 1 deployment, fits everything |

**How to deploy in Azure** (needs Azure Portal access):

1. Azure Portal → your Azure OpenAI / Foundry resource
2. Left panel → **"Model deployments"** → **"Manage Deployments"** (opens Azure AI Studio)
3. **"Create new deployment"** → pick e.g. `gpt-5-chat` (version `2025-10-03`)
4. Name the deployment (e.g. `gpt-5-chat-prod`), leave default capacity (TPM)
5. In `.env`:
   ```
   AZURE_OPENAI_API_KEY=<key from resource Keys & Endpoint>
   AZURE_OPENAI_ENDPOINT=https://<resource-name>.cognitiveservices.azure.com
   AZURE_OPENAI_DEPLOYMENT=gpt-5-chat-prod
   AZURE_OPENAI_API_VERSION=2025-01-01-preview
   ```

**If your Azure admin won't / can't deploy a dedicated model**: use
`model-router-2025-11-18` (or any `model-router-*` available). It's a
"router" — automatically routes the request to a sub-model based on
complexity. Vision payloads usually land on gpt-5-chat. The agent ships
with a built-in `fallback_llm` in case the router misroutes and returns
an empty JSON.

### 🥈 Anthropic Claude — best quality per dollar for research / one-shot runs

If you don't have Azure: Claude Sonnet 4.6 / Opus 4.7 is the **strongest**
model for the kind of complex reasoning needed for "what's the question
on the screen and what to answer". Native vision support. Cost:
~$0.05/run for a typical compliance pack.

Requires swapping `create_llm()` in `agents/course_agent.py` to
`ChatAnthropic`:

```python
from browser_use import ChatAnthropic
return ChatAnthropic(model="claude-sonnet-4-6")  # or claude-opus-4-7
```

Plus in `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

### 🥉 OpenAI direct — simplest path

No Azure, no deployments. Just a key from platform.openai.com:

```python
from browser_use import ChatOpenAI
return ChatOpenAI(model="gpt-5")  # or gpt-4o, gpt-5-chat-latest
```

`.env`: `OPENAI_API_KEY=sk-...`. Cost ~$3-8/run.

### 🪶 Google Gemini — cheapest

`gemini-2.5-flash` ~$0.01/run. Vision is OK but reasoning is weaker than
Claude / GPT-5. Good for simple, single-module courses.

```python
from browser_use import ChatGoogle
return ChatGoogle(model="gemini-2.5-flash")
```

`.env`: `GOOGLE_API_KEY=...`

---

## Time saved — concrete numbers

| Scenario | Manual | With agent | Saved |
|---|---:|---:|---:|
| **New hire — mandatory onboarding pack** (7 courses) | 7.5–10 h | 3 h wall-clock, 0 h of your work (runs in background) | **~9 hours / person** |
| **Annual compliance refresh** (Phishing + GDPR + Anti-Bribery) | 3–4 h × 200-person company = 700 h | 200 people × 0 h of work = 0 h | **~700 hours / year** |
| **Pre-ISO certification audit** (training documentation) | 2 days of manual clicking | overnight run + JSON report | **2 days / audit** |
| **New role requires 3 specialised HCM paths** | 4–6 h | 1.5 h in background | **5 hours / person** |

**Hard ROI**: 10-minute one-time setup, every person in the company saves
~9 hours × average hourly comp. For a 100-person company:
~900 hours × ~$35/h = **~$31,500 / year saved**.

More important than the money: **the agent doesn't lose focus on minute 47
of a phishing awareness slide-show**. Quiz scores are typically 100%
(when humans click "just to be done" they often score 60-80%). The
compliance officer receives deterministic, auditable results in JSON.

---

## Running — examples

```powershell
# Validation: is everything wired correctly, without launching a browser
python agents\course_agent.py --smoke-test

# Standard full platform sweep (3000 steps, 100% coverage)
python agents\course_agent.py --debug

# A specific course / development path (instead of the whole platform)
python agents\course_agent.py --url "https://acme.hcmdeck.com/protected/lessonPopup?courseId=12345"

# Smaller step budget (faster, for testing / single course)
python agents\course_agent.py --max-steps 300 --target-score 81

# Reset checkpoint (fresh sweep, ignore data/completed_courses.json)
python agents\course_agent.py --debug --reset-checkpoint

# Headless (no browser window — for server / CI)
python agents\course_agent.py --debug --headless
```

---

## Results / artefacts

After a run you'll find in `data/`:

| File | Contains |
|---|---|
| `course_run_summary.json` | Structured report: per-course status, score, blockers, actions |
| `completed_courses.json` | Durable checkpoint — remembers what's done across runs |
| `agent_run_<ts>.log` | Full DEBUG log (when `--debug`); ~200 KB–1 MB per run |
| `frame_diagnostics.jsonl` | Per-snapshot iframe/DOM diagnostics (for debugging new SCORM players) |

---

## Security

- **`.env` is in `.gitignore`** — you never commit keys
- **The agent uses YOUR Chrome with YOUR profile** — meaning it has access
  to all your signed-in accounts. Don't run it with weird prompts / prompts
  from an unknown source
- **Close Chrome before starting the agent** — Chrome locks the
  user-data-dir; concurrent access = startup error
- **SSO recovery is click-only** — the agent has a hard rule in the prompt:
  "NEVER type a password; if you see an SSO login screen, only click 'Sign
  in' and wait for the cached cookie redirect"
- **Zero exfiltration** — the agent talks exclusively with: (a) your chosen
  LLM provider, (b) your Chrome over CDP, (c) the local disk (`data/`).
  Nothing is sent to any webhooks / telemetry servers (other than
  browser-use's anonymous telemetry, which you can opt out of — see
  [browser-use docs](https://docs.browser-use.com/development/monitoring/telemetry))

---

## Common problems

| Symptom | Fix |
|---|---|
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (as admin) |
| `python` is not recognised | Reinstall Python with "Add to PATH" enabled |
| `Executable doesn't exist` / Chrome won't start | Activate venv, `uvx browser-use install` |
| `.env` not being read | Notepad saved it as `.env.txt` — enable "Show file extensions" |
| Chrome errors on profile start | You have other Chrome windows open — close ALL (including background) |
| LLM returns empty JSON | Verify `AZURE_OPENAI_DEPLOYMENT` is a multimodal model (gpt-5-chat / gpt-4o), not bare gpt-5-nano |
| Agent loops on one slide | It should detect this and fire `INTERACTIVE STALL ESCAPE`. If not — check the log for `HARD_ANTI_LOOP_STAGNATION`. Open an issue with a log snippet |
| `verify_course_completion` always NOT_FOUND | The agent must be ON the dashboard / catalog when verifying. Check the prompt — agent should return to dashboard before `verify_course_completion()` |

---

## Contributing / extending

The repo is intentionally small: one `agents/course_agent.py` with 16
custom tools + browser-use's agent loop. If you want to add support for
another LMS (Moodle, Canvas, Cornerstone, SuccessFactors), it's enough to:

1. Copy `agents/course_agent.py` to e.g. `agents/moodle_agent.py`
2. Adjust `build_task()` — replace HCM Deck UI keywords with the platform's
3. Adjust `enumerate_platform_catalog` JS selectors to the platform's DOM
4. Adjust `verify_course_completion` JS — where the platform's progress
   badge lives
5. The SCORM tools (`scorm_state`, `scorm_force`, escape tools) work
   universally — most LMSes use SCORM 2004 + iframe

Pull requests welcome.

---

## License

MIT — see [LICENSE](LICENSE). In short: do what you want, the author is
not liable. Remember that your company may have a policy against
automating mandatory training — check the rules.

---

## Acknowledgements

- [browser-use](https://github.com/browser-use/browser-use) — the library
  that makes this possible (vision LLM + CDP + tool calling in one package)
- The HCM Deck team — for a platform with working SSO that understands
  SCORM 2004
- Everyone who has ever had to click "Next" 600 times in a mandatory
  phishing awareness training

---

**Bonus**: if this agent saves you / your company 4+ hours, plz give the repo a ⭐. That's enough.
