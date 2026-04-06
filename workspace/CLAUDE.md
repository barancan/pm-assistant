# PM Assistant Workspace — CLAUDE.md

## Identity
This is a PM workspace. User is a senior product manager with 
10+ years experience, computer science background, and big data 
analytics MSc. Technical depth is expected. No hand-holding.

## Workspace Map

| Stage | Folder | Purpose |
|-------|--------|---------|
| Intake | 01_intake/ | Raw inputs. trusted/ for approved, quarantine/ for external |
| Discovery | 02_discovery/ | Raw inputs → structured pain points |
| Opportunity | 03_opportunity/ | Pain points → opportunity statements |
| PRD | 04_prd/ | Opportunity → Product Requirements Document |
| Critique | 05_critique/ | Red-team the PRD. Find gaps. Challenge assumptions. |
| Stories | 06_stories/ | PRD → User stories with acceptance criteria |

## Model Routing

| Stage | Model | Reason |
|-------|-------|--------|
| 02_discovery | Ollama gemma4:e4b | Synthesis task, local sufficient |
| 03_opportunity | Ollama gemma4:e4b | Structured framing, local sufficient |
| 04_prd | Claude API | Needs best reasoning for requirements quality |
| 05_critique | Claude API | Needs adversarial thinking quality |
| 06_stories | Ollama gemma4:e4b | Structured formatting, local sufficient |

## Golden Rules

1. One canonical source per decision. Never duplicate context across files.
2. Human reviews every stage output before the next stage runs.
3. External content always lands in quarantine/ first. Never auto-promoted.
4. CONTEXT.md files are read-only. Agents cannot modify them.
5. Stages are atomic. Each stage reads its input/, writes to its output/.
6. Output files are never deleted — they accumulate as history.

## Security Boundaries

- _core/ and _config/ are read-only to all agents
- CONTEXT.md files are read-only to all agents  
- Agents write ONLY to their designated output/ folder
- External/scraped content ALWAYS goes to quarantine/ first
