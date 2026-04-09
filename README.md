# PM Assistant

A local-first AI productivity tool for product managers. Connects to Linear, runs a structured 6-stage ICM pipeline (Intake → Discovery → Opportunity → PRD → Critique → Stories), and gives you a daily brief — all from a single-page dashboard with no cloud dependency beyond your API keys.

---

## What it does

### Daily Report

Fetches your Linear project data via the Linear GraphQL API, synthesises it with Claude, and produces a focused daily brief with four sections: **Today's Priorities**, **Blocked Items**, **Momentum**, and **Recommended Actions**.

- Run button shows contextual states: **Configure Linear →** (if key not set) / **Fetching Linear data…** (while running) / **Refresh Report · HH:MM** (after first run)
- Live token streaming while the report generates
- One-click **Send to Discovery →** to push the report into the pipeline
- **Previous Reports** — collapsible list of all past reports; click any date to render it inline

### ICM Pipeline

A 6-stage document pipeline that turns raw research into sprint-ready user stories. Stage 1 (Intake) is for file management; stages 2–6 run AI agents.

| Stage | Name | Input | Output | Model |
|-------|------|-------|--------|-------|
| 1 | Intake | External files | Promoted to Trusted | — |
| 2 | Discovery | Raw research, interviews, tickets | Structured pain points | Ollama (local) |
| 3 | Opportunity | Pain points | Opportunity statements | Ollama (local) |
| 4 | PRD | Opportunity statements | Product Requirements Document | Claude API |
| 5 | Critique | PRD draft | Red-team critique | Claude API |
| 6 | Stories | Approved PRD | User stories + acceptance criteria | Ollama (local) |

Each stage runs independently. You review output before the next stage runs — no auto-advance.

**Stage card features:**
- Status badges (idle / running / needs_review / done / error) with elapsed timer while running
- Run button shows input file count: **Run with N file(s)** or **No Input Files** (disabled) when empty
- **Manage Inputs** panel — tabs for previous-stage files, intake files, and already-queued files; drag-and-drop upload; previous-stage files pre-checked on open
- **Approve & Advance →** — marks stage done and opens the next stage's input panel in one click
- **Retry** button and inline error message when a stage errors
- Output file list with **edited** badge (amber) on files saved during the session
- Image files show 36 px thumbnails; clicking opens a full-screen **Lightbox** (Escape to close)

**Intake (Stage 1):**
- Two-column Quarantine / Trusted layout
- Promote individual quarantine files to Trusted
- Send trusted files to Discovery as a batch

### Orchestrator Chat

A Claude-powered assistant always visible at the bottom of the screen. It has awareness of your pipeline state, running processes, and latest report.

- Type natural commands (`run stage 3`, `fetch the daily report`) or ask questions
- Last 5 messages shown on focus; full history persists in SQLite (last 50 loaded on startup)
- Timestamps on every message bubble
- **Clear history** button wipes chat from the DB
- When a command triggers an action, the assistant bubble shows a spinner + label + **View →** link that navigates to the relevant panel

### Process Panel

Real-time visibility into every running and completed background task.

- Running processes: elapsed timer + **⏹ Cancel** (2-step inline confirm: *Cancel? [Yes] [No]*)
- Completed entries auto-hide after 60 s (countdown shown in the last 15 s)
- Errors persist until manually dismissed (✕ button)
- **Clear** button removes all completed entries
- Background zombie sweeper marks processes running > 10 min as errored

### Settings

Configure everything from the UI — no file editing required.

- Password fields for **Anthropic API key**, **Linear API key**, **Ollama host**, and **Ollama model**
- Masked display (shows last 4 chars of saved keys)
- **Test** button per service — live ✓ / error feedback (Anthropic: 1-token call; Linear: viewer query; Ollama: /api/tags)
- Save writes to `.env.local` and updates `os.environ` immediately — no restart needed
- **Stage Models** section: dropdown per Ollama stage (2, 3, 6) populated from pulled models; stored as `STAGE_N_MODEL` env vars

---

## Architecture

```
Browser (React SPA)
  ├── REST  ──► FastAPI (main.py)
  └── WS    ──► /ws/updates (real-time events)
                    │
        ┌──────────┼──────────┐
        │          │          │
   Orchestrator  LinearReport  ICMRunner
   (chat/intent)  Agent         Agent
        │            │             │
        └────────────┴─────────────┘
                     │
              Anthropic API / Ollama
                     │
           SQLite DB + Workspace files
```

**WebSocket events:** `process_update`, `token_stream`, `status_snapshot`, `report_complete`, `error`, `ping`

On connect the server pushes a `status_snapshot` so the UI is immediately consistent. Reconnects with exponential backoff.

### Request lifecycle

**Chat message**
1. Browser `POST /api/chat` → `main.py`
2. Orchestrator checks keyword intent (no LLM needed for routing)
3. Assembles context (process states, ICM stages, latest report) and calls Claude with the last 10 messages
4. If intent detected, `main.py` fires `_execute_action()` as a background task
5. Claude's reply returns immediately; background agent streams tokens via WebSocket

**Running an ICM stage**
1. Browser `POST /api/icm/run/{stage}` → `main.py`
2. Process record created in SQLite; `ICMRunnerAgent` starts as background task
3. Agent reads `CONTEXT.md` as system prompt, all input `.md` files as user message
4. Calls Ollama (stages 2, 3, 6) or Claude (stages 4, 5), streaming tokens via WebSocket
5. Writes `draft_{timestamp}.md` to `{stage}/output/`, sets stage status to `needs_review`

**Daily report**
1. Browser `POST /api/agents/linear-report/run` → `main.py`
2. `LinearReportAgent` fetches Linear via GraphQL (projects, active issues, priority, recent movement)
3. Sends to Claude for synthesis; tokens stream to browser
4. Report saved to SQLite and written to `workspace/01_intake/trusted/daily_report.md`

### Agent security model

Every agent inherits from `BaseAgent`, which enforces a 3-layer path check on every file operation:

| Layer | Rule |
|-------|------|
| 1 | Path must resolve inside `WORKSPACE` (no `../` escapes) |
| 2 | Path must not match `READONLY_PATTERNS` (`CLAUDE.md`, `CONTEXT.md`, `_core/`, `_config/`) |
| 3 | Path must match both `WRITABLE_PATTERNS` (global allow-list) and `AGENT_ALLOWED_WRITES` (per-agent allow-list) |

Read operations enforce layer 1 only. Writes enforce all three. The Discovery agent, for example, can only write to `02_discovery/output/` regardless of what paths it receives.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, aiosqlite, Anthropic SDK, httpx, python-dotenv |
| Frontend | Single HTML file, React 18 via CDN, Babel Standalone, marked.js v9 (local bundle) |
| Models | `claude-sonnet-4-6` for PRD, Critique, report synthesis, chat; Ollama (configurable) for Discovery, Opportunity, Stories |
| Storage | SQLite (`backend/pm_assistant.db`), workspace markdown files |

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai) running locally with at least one model pulled (e.g. `gemma3:4b`)
- Anthropic API key
- Linear API key (optional — enables the daily report feature)

---

## Setup

```bash
git clone https://github.com/barancan/pm-assistant.git
cd pm-assistant

# Copy and fill in your credentials (or use the Settings UI after first launch)
cp .env.example .env.local
# Add ANTHROPIC_API_KEY and optionally LINEAR_API_KEY

./start.sh   # checks Python version, installs deps, starts server, opens browser
```

The app runs at `http://localhost:3000`.

All API keys can also be configured from the **Settings** panel in the UI — no file editing needed after first launch.

---

## Configuration

`.env.local` is never committed. `.env` ships with the repo and contains only placeholder values. `.env.local` overrides it.

```
ANTHROPIC_API_KEY=sk-ant-...
LINEAR_API_KEY=lin_api_...         # optional
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma3:4b             # default fallback model
STAGE_2_MODEL=gemma3:4b            # per-stage model override (set from Settings UI)
STAGE_3_MODEL=gemma3:4b
STAGE_6_MODEL=gemma3:4b
WORKSPACE_PATH=./workspace
DATABASE_PATH=./backend/pm_assistant.db
```

---

## Workspace structure

```
workspace/
├── CLAUDE.md               # Root orientation file (read-only)
├── _core/
│   └── pm_principles.md    # PM framework reference (read-only)
├── _config/
│   └── model_routing.md    # Model routing rules (read-only)
├── 01_intake/
│   ├── trusted/            # Reviewed inputs; daily reports land here
│   └── quarantine/         # External/untrusted content — promote manually
├── 02_discovery/
│   ├── CONTEXT.md          # System prompt for stage 2 (read-only)
│   ├── input/              # Files queued for the next run
│   └── output/             # Timestamped drafts accumulate here
├── 03_opportunity/
├── 04_prd/
├── 05_critique/
└── 06_stories/
```

To run a stage: add `.md` files to its `input/` folder via the **Manage Inputs** panel, then click **Run with N file(s)** or type `run stage N` in the chat.

---

## Key files

| File | Purpose |
|------|---------|
| `backend/main.py` | FastAPI app, WebSocket manager, all REST endpoints |
| `backend/database.py` | aiosqlite, 4 tables: processes, reports, chat_messages, icm_stages |
| `backend/orchestrator.py` | Claude-powered chat with keyword intent detection + context assembly |
| `backend/agents/base_agent.py` | Security, Ollama + Claude calling, streaming, per-stage model env var |
| `backend/agents/icm_runner.py` | Runs stages 2–6, reads CONTEXT.md as system prompt |
| `backend/agents/linear_report.py` | Fetches Linear via GraphQL, synthesises with Claude |
| `frontend/index.html` | ~2700-line single-file React app (Spotify-dark theme) |
| `frontend/marked.min.js` | Local copy of marked.js v9, served at `/static/marked.min.js` |

---

## Security

The primary security boundary is code, not filesystem permissions. See [Agent security model](#agent-security-model) above.

### Optional: lock configuration files

```bash
# Prevent accidental writes to orientation and config files
chmod 444 workspace/CLAUDE.md
chmod 444 workspace/_core/pm_principles.md
chmod 444 workspace/_config/model_routing.md
chmod 444 workspace/0{2,3,4,5,6}_*/CONTEXT.md
```

To temporarily edit a `CONTEXT.md` (e.g. tune stage instructions):

```bash
chmod 644 workspace/04_prd/CONTEXT.md
# edit the file
chmod 444 workspace/04_prd/CONTEXT.md
```

Do not chmod the `input/` directories — the server writes to them when you promote files through the UI.

---

## License

MIT
