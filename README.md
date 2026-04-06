# PM Assistant

A local-first AI productivity tool for senior product managers. Connects to Linear, runs a structured ICM pipeline (Discovery → Opportunity → PRD → Critique → Stories), and gives you a daily brief — all from a single-page UI with no cloud dependency beyond your API keys.

---

## What it does

**Daily Report** — Fetches your Linear project data, synthesises it with Claude, and produces a focused daily brief: today's priorities, blocked items, momentum, and recommended actions.

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

## Stack

- **Backend** — Python 3.11, FastAPI, aiosqlite, Anthropic SDK, httpx
- **Frontend** — Single HTML file, React 18 via CDN, no build step
- **Models** — Claude (claude-sonnet-4-6) for quality-critical tasks, Ollama (gemma3:4b or similar) for local tasks
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
cp .env.example .env
# Edit .env — add ANTHROPIC_API_KEY and optionally LINEAR_API_KEY

# Start (checks Python version, installs deps, opens browser)
./start.sh
```

The app runs at `http://localhost:3000`.

---

## Configuration

All configuration is in `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma3:4b          # or any model you have pulled
WORKSPACE_PATH=./workspace
DATABASE_PATH=./backend/pm_assistant.db
LINEAR_MCP_URL=https://mcp.linear.app/mcp
LINEAR_API_KEY=lin_api_...      # optional
```

---

## Workspace structure

```
workspace/
├── 01_intake/
│   ├── trusted/        # Reviewed inputs, daily reports land here
│   └── quarantine/     # External/untrusted content — never auto-promoted
├── 02_discovery/
│   ├── input/          # Drop research files here before running stage 2
│   └── output/         # Stage outputs accumulate here (never deleted)
├── 03_opportunity/
├── 04_prd/
├── 05_critique/
└── 06_stories/
```

To run a stage: add `.md` files to its `input/` folder, then click **Run Stage** in the UI or type `run stage N` in the chat.

---

## Security

- Agents can only write to their designated `output/` folder
- `CLAUDE.md`, `CONTEXT.md`, `_core/`, and `_config/` are read-only to all agents
- External content goes to `01_intake/quarantine/` — never auto-promoted to `trusted/`
- No credentials are ever written to the workspace

### Locking workspace files (recommended)

Run these in order from inside your `pm-assistant/` directory:

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

# Intake folders — quarantine writable by agents, trusted writable by you
chmod 755 workspace/01_intake/quarantine
chmod 755 workspace/01_intake/trusted
```

Then verify it worked:

```bash
# This should print "Permission denied"
echo "test" >> workspace/CLAUDE.md

# This should succeed (you can still read)
cat workspace/CLAUDE.md
```

**One important note:** When you need to edit a `CONTEXT.md` file yourself in the future — to improve a stage's instructions — you'll need to temporarily unlock it first:

```bash
# To edit
chmod 644 workspace/04_prd/CONTEXT.md

# Edit the file, then re-lock
chmod 444 workspace/04_prd/CONTEXT.md
```

---

## License

MIT
