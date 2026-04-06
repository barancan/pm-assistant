# PM Assistant

A local-first AI productivity tool for product managers. Connects to Linear, runs a structured ICM pipeline (Discovery → Opportunity → PRD → Critique → Stories), and gives you a daily brief — all from a single-page UI with no cloud dependency beyond your API keys.

---

## What it does

**Daily Report** — Fetches your Linear project data via the Linear GraphQL API, synthesises it with Claude, and produces a focused daily brief: today's priorities, blocked items, momentum, and recommended actions.

**ICM Pipeline** — A 5-stage document pipeline that turns raw research into sprint-ready user stories:

| Stage | Input | Output | Model |
|-------|-------|--------|-------|
| 2 · Discovery | Raw research, interviews, tickets | Structured pain points | Ollama (local) |
| 3 · Opportunity | Pain points | Opportunity statements | Ollama (local) |
| 4 · PRD | Opportunity statements | Product Requirements Document | Claude API |
| 5 · Critique | PRD draft | Red-team critique | Claude API |
| 6 · Stories | Approved PRD | User stories + acceptance criteria | Ollama (local) |

Each stage runs independently. You review the output before the next stage runs — no auto-advance.

**Orchestrator Chat** — A Claude-powered assistant with awareness of your pipeline state, running processes, and latest report. Type natural language commands (`run stage 3`, `run the daily report`) or ask questions.

---

## Architecture

```
Browser (React SPA)
      │
      ├── REST  ──► FastAPI (main.py)
      └── WS    ──► /ws/updates  (real-time events)
                         │
              ┌──────────┼──────────┐
              │          │          │
       Orchestrator  LinearReport  ICMRunner
       (chat, intent   Agent        Agent
        detection)     │            │
              │         │           │
              └────┬────┘           │
                   │                │
            Anthropic API      Ollama / Anthropic API
            (claude-sonnet)    (per stage model map)
                   │
              SQLite DB  ◄──── all agents write status here
              Workspace  ◄──── agents read/write markdown files
```

### Request lifecycle

**Chat message**
1. Browser `POST /api/chat` → `main.py`
2. `main.py` calls `Orchestrator.chat()` which checks keyword intent (no LLM needed for routing)
3. Orchestrator assembles context (process states, ICM stages, latest report) and calls Claude with the last 10 messages as history
4. If intent was detected (e.g. "run stage 3"), `main.py` fires `_execute_action()` as a background task
5. Claude's reply is returned to the browser immediately; the background agent streams tokens back via WebSocket

**Running an ICM stage**
1. Browser `POST /api/icm/run/{stage}` (or triggered from chat) → `main.py`
2. `main.py` creates a process record in SQLite and starts `ICMRunnerAgent` as a background task
3. Agent reads `CONTEXT.md` for the stage as its system prompt
4. Agent reads all `.md` files from `{stage}/input/` and combines them as the user message
5. Agent calls Ollama (stages 2, 3, 6) or Claude API (stages 4, 5), streaming tokens back via WebSocket
6. Agent writes `draft_{timestamp}.md` to `{stage}/output/` and updates the stage status to `needs_review`

**Daily report**
1. Browser `POST /api/agents/linear-report/run` (or triggered from chat) → `main.py`
2. `LinearReportAgent` calls the Linear GraphQL API (`api.linear.app/graphql`) to fetch projects, active issues, priority issues, in-progress items, and recent movement
3. Formatted data is sent to Claude for synthesis; tokens stream to the browser
4. Final report is saved to SQLite and written to `workspace/01_intake/trusted/daily_report.md`

**File viewer / editor**
1. When a stage card is expanded, browser `GET /api/workspace/files?dir={stage}/input` and `.../output`
2. Clicking a file: `GET /api/workspace/file?path={relative}` — read anywhere in workspace
3. Saving edits: `PUT /api/workspace/file` — restricted to output and intake directories only

**WebSocket**
- Connected on page load; receives `process_update`, `token_stream`, `report_complete`, `error`, and `ping` events
- On connect, server sends a full `status_snapshot` so the UI is immediately consistent
- Reconnects automatically with exponential backoff if the connection drops

### Agent security model

Every agent inherits from `BaseAgent`, which enforces a 3-layer path check on every file operation:

| Layer | Rule |
|-------|------|
| 1 | Path must resolve inside `WORKSPACE` (no `../` escapes) |
| 2 | Path must not match `READONLY_PATTERNS` (`CLAUDE.md`, `CONTEXT.md`, `_core/`, `_config/`) |
| 3 | Path must match both `WRITABLE_PATTERNS` (global allow-list) and `AGENT_ALLOWED_WRITES` (per-agent allow-list) |

Read operations only enforce layer 1. Write operations enforce all three.

The browser file editor mirrors this: reads are workspace-wide; writes are additionally validated against the same `WRITABLE_PATTERNS` list in `main.py`.

---

## Stack

- **Backend** — Python 3.11+, FastAPI, aiosqlite, Anthropic SDK, httpx
- **Frontend** — Single HTML file, React 18 via CDN, no build step
- **Models** — Claude (`claude-sonnet-4-6`) for quality-critical tasks, Ollama (configurable model) for local tasks
- **Storage** — SQLite (local file), workspace markdown files

---

## Requirements

- Python 3.11+
- [Ollama](https://ollama.ai) running locally with your preferred model pulled
- Anthropic API key (for PRD, Critique, daily report synthesis, and chat)
- Linear API key (optional — needed for the daily report feature)

---

## Setup

```bash
git clone https://github.com/barancan/pm-assistant.git
cd pm-assistant

# Copy and fill in your credentials
cp .env.example .env.local
# Edit .env.local — add ANTHROPIC_API_KEY and optionally LINEAR_API_KEY

# Start (checks Python version, installs deps, opens browser)
./start.sh
```

The app runs at `http://localhost:3000`.

---

## Configuration

All configuration is in `.env.local` (never committed):

```
ANTHROPIC_API_KEY=sk-ant-...
LINEAR_API_KEY=lin_api_...      # optional — enables daily report
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma3:4b          # or any model you have pulled
WORKSPACE_PATH=./workspace
DATABASE_PATH=./backend/pm_assistant.db
```

`.env` ships with the repo and contains only placeholder values. `.env.local` overrides it and is gitignored.

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
│   └── quarantine/         # External/untrusted content — never auto-promoted
├── 02_discovery/
│   ├── CONTEXT.md          # System prompt for stage 2 (read-only)
│   ├── input/              # Drop research files here before running stage 2
│   └── output/             # Timestamped drafts accumulate here
├── 03_opportunity/
├── 04_prd/
├── 05_critique/
└── 06_stories/
```

To run a stage: add `.md` files to its `input/` folder, then click **Run Stage** in the UI or type `run stage N` in the chat. Output files are viewable and editable directly in the dashboard.

---

## Security

- Agents can only write to their designated `output/` folder
- `CLAUDE.md`, `CONTEXT.md`, `_core/`, and `_config/` are read-only to all agents
- External content goes to `01_intake/quarantine/` — never auto-promoted to `trusted/`
- No credentials are ever written to the workspace

### Locking workspace files (recommended)

Run these from inside your `pm-assistant/` directory:

```bash
# Lock the root orientation file
chmod 444 workspace/CLAUDE.md

# Lock all core and config files
chmod 444 workspace/_core/pm_principles.md
chmod 444 workspace/_config/model_routing.md

# Lock all CONTEXT.md files across every stage
chmod 444 workspace/02_discovery/CONTEXT.md
chmod 444 workspace/03_opportunity/CONTEXT.md
chmod 444 workspace/04_prd/CONTEXT.md
chmod 444 workspace/05_critique/CONTEXT.md
chmod 444 workspace/06_stories/CONTEXT.md

# Stage input folders — agents cannot write here
chmod 555 workspace/02_discovery/input
chmod 555 workspace/03_opportunity/input
chmod 555 workspace/04_prd/input
chmod 555 workspace/05_critique/input
chmod 555 workspace/06_stories/input

# Stage output folders — agents write here
chmod 755 workspace/02_discovery/output
chmod 755 workspace/03_opportunity/output
chmod 755 workspace/04_prd/output
chmod 755 workspace/05_critique/output
chmod 755 workspace/06_stories/output

# Intake folders
chmod 755 workspace/01_intake/quarantine
chmod 755 workspace/01_intake/trusted
```

Then verify:

```bash
# This should print "Permission denied"
echo "test" >> workspace/CLAUDE.md

# This should succeed
cat workspace/CLAUDE.md
```

To temporarily edit a `CONTEXT.md` file (e.g. to tune stage instructions):

```bash
chmod 644 workspace/04_prd/CONTEXT.md
# edit the file
chmod 444 workspace/04_prd/CONTEXT.md
```

---

## License

MIT
