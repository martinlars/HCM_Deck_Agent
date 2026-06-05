# HCM Deck Course Agent

> **Twoja firma kazała Ci zaliczyć 7 obowiązkowych szkoleń compliance do końca tygodnia?**
> Każde po 90 minut. SCORM, quiz, NPS feedback, "kliknij każdy hotspot żeby przejść dalej".
> Łącznie ~10 godzin klikania. Ten agent zrobi to za Ciebie w **3 godziny w tle** — Ty
> w tym czasie pracujesz, a wieczorem masz wszystkie kursy ✅ zaliczone na 100%.

Autonomiczny agent przeglądarki dla platformy **HCM Deck** (popularny w Polsce
LMS używany przez wiele dużych firm do obowiązkowych szkoleń compliance).
Loguje się przez SSO Twojego firmowego
Microsoft / Google konta, znajduje **wszystkie** kursy/testy/szkolenia na platformie,
przerabia je strona po stronie, rozwiązuje quizy (z retry przy błędnych odpowiedziach),
zatwierdza NPS feedback i weryfikuje że dashboard naprawdę pokazuje 100% — nie tylko
że SCORM player wyświetlił "Bye!".

Zbudowany na [browser-use](https://github.com/browser-use/browser-use) (open-source).
Pracuje w **Twoim** Chrome z **Twoimi** zalogowanymi sesjami — nie wpisuje haseł,
nie wycieka cookies, niczego nie wysyła poza wybranego dostawcę LLM.

---

## Dlaczego to istnieje (kontekst z prawdziwego runu)

Typowy korporacyjny pakiet onboardingowy / compliance to:

| Kurs | Czas ręcznie | Co tam jest |
|---|---|---|
| Phishing Awareness | 30–45 min | slajdy + 10-pytaniowy test |
| Anti-Corruption Policy | 60 min | multi-modułowe, hotspoty, quiz |
| Mobbing & Discrimination | 90 min | 3 sekcje × 22 strony + quiz + NPS |
| Working remotely | 60 min | interaktywne hotspoty, quiz |
| Inclusive Language | 75 min | 3 moduły × kilkanaście slajdów |
| Rules of feedback | 60 min | 5 sekcji + accordeony + video + quiz |
| Mandatory Poland path | 90 min | ścieżka 3 powiązanych kursów |
| **RAZEM** | **~7,5–10h** | ~600 kliknięć, ~50 odpowiedzi w quizach |

**Run agenta na powyższym pakiecie**: 56 kroków LLM, 0 błędów, **100% zaliczone, ~3h
clock time** (w tym czasie komputer pracuje, Ty robisz co innego). Realne dane z
ostatniego runu — `data/course_run_summary.json`.

### Co dokładnie ten agent umie czego nie umieją inne "browser bots"

- **Vision-first nawigacja przez cross-origin SCORM iframes** (OOPIF CDP attach
  per-frame) — SCORM-y na HCM Deck są w nested iframe na innym origin, klasyczne
  DOM-tools są ślepe. Agent zrzuca screenshot, pyta LLM "gdzie jest NASTĘPNY",
  klika przez CDP coordinate click.
- **Interactive Stall Escape Protocol** — gdy slajd ma canvas/SVG/drag-and-drop
  interaktyw którego ani DOM, ani vision nie ruszają (np. "kliknij każdy z 4
  punktów na mapie"), agent uruchamia kaskadę: brute-grid (24 punkty kliknięcia
  6×4) → keypress (Space/Enter/ArrowRight) → drag-drop probe → page navigation
  fallback. To uratowało Mobbing course, który poprzedni run zablokował na
  Page 10/22.
- **Anti-lie completion verification** — `verify_course_completion()` przed każdym
  zapisaniem SUCCESS-u wraca na dashboard i czyta REALNY badge progress (%,
  "Ukończone", `aria-valuenow` na progress barze). `mark_course_done(SUCCESS)`
  jest twardo odrzucone bez verified=True. Ten guard wziął się z prawdziwego
  bugu: agent zobaczył "End of module 1" w trzymodułowym kursie, uznał za
  100% — dashboard pokazywał 33%. Teraz to niemożliwe.
- **Signature-based anti-loop** — klasyczny "ten sam tool z tymi samymi args
  ≥4 razy" + drugi guard: "≥8 wywołań DOWOLNYCH narzędzi bez zmiany snapshot
  signature ⇒ HARD_ANTI_LOOP_STAGNATION + wymuszenie escape protocol".
- **Durable checkpoint** — każdy ukończony kurs zapisywany do
  `data/completed_courses.json` od razu po `mark_course_done`. Awaria
  agenta po 2 godzinach nie traci zrobionej roboty — ponowny start wczytuje
  i pomija to co już zrobione.
- **Strukturalny output** (Pydantic) — gotowy raport JSON z per-course
  scoringiem, statusem, blockerami i listą podjętych akcji. Wpinasz do
  ServiceNow / Jiry / dashboardu reportingowego bez parsowania tekstu.
- **3000-stepowy budget + fallback LLM** — długie kursy nie wykolejają runu.
  Drugi profil LLM (minimal effort, temp 0) wskakuje przy pustym JSON-ie
  z model-routera Azure.

---

## Architektura w 60 sekund

```
┌─────────────────────────────────────────────────────────────┐
│  Twój Chrome (Twój profil, Twoje SSO cookies)               │
│  ┌─ HCM Deck dashboard ─┐  ┌─ SCORM popup ────────────────┐ │
│  │ Do zrobienia (7)     │  │  runLessonContainer iframe   │ │
│  │ W trakcie  (3)       │  │   └─ lessonFrame (OOPIF)     │ │
│  │ Katalog    (12)      │  │       └─ Twój SCORM content  │ │
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
                  │ Azure OpenAI        │  ← REKOMENDOWANE
                  │   gpt-5-chat / 4o   │
                  │  lub Anthropic /    │
                  │  OpenAI / Gemini    │
                  └─────────────────────┘
```

---

## Instalacja — 5 kroków, 10 minut

### Wymagania wstępne

- **Windows 10/11** (Linux/macOS będą działać po drobnych zmianach ścieżek
  Chrome — patrz `agents/course_agent.py:53-57`)
- **Python 3.11 lub 3.12** — pobierz z [python.org](https://www.python.org/downloads/)
  lub `winget install Python.Python.3.12`. Zaznacz "Add to PATH".
- **Google Chrome** zainstalowany z Twoim firmowym SSO już zalogowanym
- **Klucz API do LLM** (Azure OpenAI / Anthropic / OpenAI / Google — patrz dalej)

### Krok 1 — sklonuj repo i wejdź do folderu

```powershell
git clone https://github.com/<TWOJ-USER>/hcm-deck-agent.git
cd hcm-deck-agent
```

### Krok 2 — odblokuj PowerShell (jednorazowo)

W PowerShell **jako administrator**:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Jeśli to problem — użyj `install.bat` zamiast `install.ps1`.

### Krok 3 — uruchom instalator

```powershell
.\install.ps1
```

Instalator zrobi: weryfikacja Pythona → instalacja `uv` → utworzenie `.venv` →
instalacja zależności → pobranie Chromium (~130 MB; pomijalne jeśli używasz
własnego Chrome) → utworzenie `.env` z szablonu.

### Krok 4 — wypełnij `.env`

```powershell
notepad .env
```

Trzy rzeczy są obowiązkowe:

1. `HCM_DASHBOARD_URL` — np. `https://acme.hcmdeck.com/protected/home`
2. **Klucze LLM** — patrz [LLM configuration](#llm-configuration--co-rekomendujemy)
3. `CHROME_EXE` / `CHROME_USER_DATA` — tylko jeśli Chrome jest w niestandardowym
   miejscu (domyślne wartości pasują do typowej instalacji Windows)

### Krok 5 — uruchom agenta

```powershell
.\.venv\Scripts\activate
python agents\course_agent.py --smoke-test       # walidacja konfiguracji
python agents\course_agent.py --debug            # pełny run, 3000 kroków
```

Polecam pierwszy run zrobić jak idziesz na obiad / mityng / spać. Agent pracuje
w tle, możesz monitorować postęp w `data/agent_run_<ts>.log`.

---

## LLM configuration — co rekomendujemy

Z czego korzystaliśmy podczas budowy i co rekomendujemy:

### 🥇 Azure OpenAI / Foundry — REKOMENDOWANE dla firm

Najlepszy stosunek jakości do kosztów, jeśli Twoja organizacja już ma
Azure OpenAI resource. Co ważne — przy SSO Twojego firmowego konta nie
wychodzisz poza tenanta z żadnymi danymi.

**Najlepsze deploymenty (kolejność preferencji)**:

| Model | Typ | Czemu | Uwagi |
|---|---|---|---|
| **`gpt-5-chat-2025-10-03`** | multimodal flagship preview | Najlepszy do vision-heavy UI nav | Wymaga deploymentu, ~$2-5/run |
| **`gpt-5-chat-2025-08-07`** | multimodal preview (older) | Sprawdzony, stabilny | Wymaga deploymentu |
| **`gpt-4o-2024-11-20`** | proven multimodal | Niezawodny baseline, GA | Wymaga deploymentu |
| **`model-router-2025-11-18`** | router GA (newest) | Auto-route do gpt-5-chat dla vision | Najprostszy: 1 deployment na wszystko |

**Jak zdeployować w Azure** (potrzebny dostęp do Azure Portal):

1. Wejdź do Azure Portal → swój Azure OpenAI / Foundry resource
2. Lewy panel → **"Model deployments"** → **"Manage Deployments"** (otwiera Azure AI Studio)
3. **"Create new deployment"** → wybierz np. `gpt-5-chat` (wersja `2025-10-03`)
4. Nazwij deployment (np. `gpt-5-chat-prod`), zostaw default capacity (TPM)
5. W `.env`:
   ```
   AZURE_OPENAI_API_KEY=<klucz_z_resource_keys_and_endpoint>
   AZURE_OPENAI_ENDPOINT=https://<resource-name>.cognitiveservices.azure.com
   AZURE_OPENAI_DEPLOYMENT=gpt-5-chat-prod
   AZURE_OPENAI_API_VERSION=2025-01-01-preview
   ```

**Jeśli admin Azure nie chce/nie może zdeployować dedykowanego modelu**: użyj
`model-router-2025-11-18` (lub jakikolwiek model-router-* dostępny). To
"router" — automatycznie routuje request do podmodelu w zależności od
złożoności. Vision payload zwykle ląduje na gpt-5-chat. Agent ma wbudowany
`fallback_llm` na wypadek gdy router się pomyli i zwróci pusty JSON.

### 🥈 Anthropic Claude — najlepsza jakość per dolar dla research/jednorazowych runów

Jeśli nie masz Azure: Claude Sonnet 4.6 / Opus 4.7 jest **najsilniejszy** w
złożonym reasoningu typu "jakie pytanie jest na ekranie i co odpowiedzieć".
Natywna obsługa vision. Cena: ~$0.05/run dla typowego pakietu compliance.

Wymaga zamiany `create_llm()` w `agents/course_agent.py` na `ChatAnthropic`:

```python
from browser_use import ChatAnthropic
return ChatAnthropic(model="claude-sonnet-4-6")  # albo claude-opus-4-7
```

Plus w `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

### 🥉 OpenAI direct — najprostsza ścieżka

Bez Azure, bez deploymentów. Po prostu klucz z platform.openai.com:

```python
from browser_use import ChatOpenAI
return ChatOpenAI(model="gpt-5")  # albo gpt-4o, gpt-5-chat-latest
```

`.env`: `OPENAI_API_KEY=sk-...`. Koszt ~$3-8/run.

### 🪶 Google Gemini — najtańszy

`gemini-2.5-flash` ~$0.01/run. Vision OK ale słabszy reasoning od Claude/GPT-5.
Dobry do prostych, jednomodułowych kursów.

```python
from browser_use import ChatGoogle
return ChatGoogle(model="gemini-2.5-flash")
```

`.env`: `GOOGLE_API_KEY=...`

---

## Oszczędność czasu — konkretne liczby

| Scenariusz | Ręcznie | Z agentem | Oszczędność |
|---|---:|---:|---:|
| **Nowy pracownik — obowiązkowy onboarding pack** (7 kursów) | 7.5–10 h | 3 h wall-clock, 0 h pracy (działa w tle) | **~9 godzin / osoba** |
| **Roczna rotacja compliance** (Phishing + RODO + Anti-Bribery odświeżenie) | 3–4 h × cała firma 200 osób = 700 h | 200 osób × 0 h pracy = 0 h | **~700 godzin / rok** |
| **Audyt przed certyfikacją ISO** (dokumentacja przeszkolenia) | 2 dni manualnego klikania | nocny run + JSON raport | **2 dni / audyt** |
| **Nowa rola wymaga 3 specjalistycznych ścieżek HCM** | 4–6 h | 1.5 h w tle | **5 godzin / osoba** |

**Twardy zwrot z inwestycji**: jednorazowo 10 minut setupu, każda osoba w firmie
oszczędza średnio 9 godzin × średnie wynagrodzenie. Dla firmy 100-osobowej:
~900 godzin × ~150 zł/h = **~135,000 zł / rok zaoszczędzone**.

Co ważniejsze niż pieniądze: **agent nie traci koncentracji w 47. minucie
phishing-awareness slajdshow**. Quiz score zwykle 100% (przy ręcznym klikaniu
"żeby było" pracownicy często dostają 60-80%). Compliance officer dostaje
deterministyczne, audytowalne wyniki w JSON.

---

## Uruchamianie — przykłady

```powershell
# Walidacja: czy wszystko dobrze podpięte, bez uruchamiania przeglądarki
python agents\course_agent.py --smoke-test

# Standardowy full-sweep platformy (3000 kroków, 100% pokrycia)
python agents\course_agent.py --debug

# Konkretny kurs / ścieżka rozwoju (zamiast całej platformy)
python agents\course_agent.py --url "https://acme.hcmdeck.com/protected/lessonPopup?courseId=12345"

# Mniejszy budget kroków (szybciej, dla testów / pojedynczego kursu)
python agents\course_agent.py --max-steps 300 --target-score 81

# Reset checkpointu (świeży sweep, ignoruj data/completed_courses.json)
python agents\course_agent.py --debug --reset-checkpoint

# Headless (bez okna przeglądarki — dla servera / CI)
python agents\course_agent.py --debug --headless
```

---

## Wyniki / artefakty

Po runie znajdziesz w `data/`:

| Plik | Co zawiera |
|---|---|
| `course_run_summary.json` | Strukturalny raport: per-course status, score, blockery, akcje |
| `completed_courses.json` | Durable checkpoint — pamięta co już zrobione między runami |
| `agent_run_<ts>.log` | Pełny DEBUG log (gdy `--debug`); ~200 KB–1 MB per run |
| `frame_diagnostics.jsonl` | Per-snapshot diagnostyka iframe/DOM (do debugowania nowych SCORM playerów) |

---

## Bezpieczeństwo

- **`.env` jest w `.gitignore`** — nigdy nie commitujesz kluczy
- **Agent używa Twojego Chrome z Twoim profilem** — to znaczy ma dostęp do
  wszystkich Twoich zalogowanych kont. Nie uruchamiaj go z dziwnymi promptami /
  na promptach z nieznanego źródła
- **Zamknij Chrome przed startem agenta** — Chrome locks user-data-dir,
  jednoczesny dostęp = błąd startu
- **SSO recovery only by click** — agent ma w prompt twardą regułę "NIGDY nie
  wpisuj hasła; jeśli widzisz SSO login screen, klikaj tylko 'Zaloguj' i czekaj
  na cached cookie redirect"
- **Zero exfiltracji** — agent gada wyłącznie z: (a) wybranym dostawcą LLM,
  (b) Twoim Chrome przez CDP, (c) lokalnym diskiem (`data/`). Nic nie wysyła
  na żadne webhooki / telemetry serwery (oprócz anonimowej telemetrii browser-use
  którą można wyłączyć — patrz [browser-use docs](https://docs.browser-use.com/development/monitoring/telemetry))

---

## Częste problemy

| Objaw | Rozwiązanie |
|---|---|
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (jako admin) |
| `python` nie jest rozpoznawane | Reinstaluj Pythona z włączoną "Add to PATH" |
| `Executable doesn't exist` / Chrome nie startuje | Aktywuj venv, `uvx browser-use install` |
| `.env` nie czytany | Notepad zapisał jako `.env.txt` — włącz "Pokaż rozszerzenia plików" |
| Chrome błąd przy starcie z profilem | Masz otwarte okna Chrome — zamknij WSZYSTKIE (włącznie z background) |
| LLM zwraca puste JSON-y | Sprawdź czy `AZURE_OPENAI_DEPLOYMENT` to model multimodalny (gpt-5-chat / gpt-4o), nie samo gpt-5-nano |
| Agent kręci się na slajdzie | Powinien wykryć i odpalić `INTERACTIVE STALL ESCAPE`. Jeśli nie — sprawdź w logu czy `HARD_ANTI_LOOP_STAGNATION` się odpalił. Zgłoś issue z fragmentem logu |
| `verify_course_completion` zawsze NOT_FOUND | Agent musi być NA dashboardzie / catalog gdy weryfikuje. Sprawdź prompt — agent powinien wrócić do dashboard przed `verify_course_completion()` |

---

## Wkład / rozszerzanie

Repo jest celowo małe: jedno `agents/course_agent.py` z 16 toolami custom +
agent loop browser-use. Jeśli chcesz dodać support dla innego LMS-a
(Moodle, Canvas, Cornerstone, SuccessFactors), wystarczy:

1. Skopiować `agents/course_agent.py` na np. `agents/moodle_agent.py`
2. Dostosować `build_task()` — Polish UI keywords → keywords platformy
3. Dostosować `enumerate_platform_catalog` JS selectors do struktury DOM
4. Dostosować `verify_course_completion` JS — gdzie tam jest progress badge
5. SCORM tools (`scorm_state`, `scorm_force`, escape tools) działają
   uniwersalnie — większość LMS-ów używa SCORM 2004 + iframe

Pull requesty mile widziane.

---

## Licencja

MIT — patrz [LICENSE](LICENSE). Krótko: rób co chcesz, autor nie ponosi
odpowiedzialności. Pamiętaj że Twoja firma może mieć policy zabraniające
automatyzacji obowiązkowych szkoleń — sprawdź regulamin.

---

## Podziękowania

- [browser-use](https://github.com/browser-use/browser-use) — biblioteka która
  to wszystko robi możliwym (vision LLM + CDP + tool calling w jednym pakiecie)
- HCM Deck team — za platformę która ma działający SSO i rozumie SCORM 2004
- Każdy kto kiedykolwiek musiał kliknąć "Next" 600 razy w obowiązkowym
  szkoleniu o phishingu

---

**Bonus**: jeśli ten agent zaoszczędzi Tobie/Twojej firmie 100+ godzin —
postaw mi kawę przez [GitHub Sponsors](https://github.com/sponsors). Albo
po prostu daj ⭐ repo. To wystarczy.
