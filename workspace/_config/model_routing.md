# Model Routing Configuration

## Local Models (Ollama)
Host: configured via OLLAMA_HOST env var
Default model: gemma4:e4b
Fallback: If Ollama unavailable, fail explicitly — do not silently use cloud

## Cloud Models (Claude API)  
Model: claude-sonnet-4-6
Used for: stages requiring highest reasoning quality (PRD, Critique)
and orchestration (daily report synthesis, chat)

## Routing Rules
- Cost-sensitive tasks → Ollama local
- Quality-critical tasks → Claude API
- Real-time streaming → both support it, prefer local for speed
- Batch background tasks → Ollama preferred
