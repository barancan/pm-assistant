import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env then .env.local (overrides) before importing database / agents
_root = Path(__file__).parent.parent
load_dotenv(dotenv_path=_root / ".env")
load_dotenv(dotenv_path=_root / ".env.local", override=True)

import database
from orchestrator import Orchestrator
from agents.linear_report import LinearReportAgent
from agents.icm_runner import ICMRunnerAgent

_project_root = Path(__file__).parent.parent
_raw_ws = os.environ.get("WORKSPACE_PATH", "./workspace")
WORKSPACE_PATH = (_project_root / _raw_ws.lstrip("./")).resolve() \
    if _raw_ws.startswith("./") or _raw_ws.startswith("../") \
    else Path(_raw_ws).resolve()


# ─── WebSocket Connection Manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
orchestrator = Orchestrator()


async def broadcast_update(event_type: str, data: dict) -> None:
    await manager.broadcast({"type": event_type, **data})


# ─── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Resolve DATABASE_PATH relative to the project root (parent of backend/)
    # so it works regardless of the CWD when uvicorn starts
    project_root = Path(__file__).parent.parent
    raw_db_path = os.environ.get("DATABASE_PATH", "./backend/pm_assistant.db")
    db_path = str((project_root / raw_db_path.lstrip("./")).resolve()
                  if raw_db_path.startswith("./") or raw_db_path.startswith("../")
                  else Path(raw_db_path).resolve())
    # Ensure the parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    database.set_db_path(db_path)

    # Update BaseAgent workspace path from env
    from agents.base_agent import BaseAgent
    BaseAgent.WORKSPACE = WORKSPACE_PATH

    await database.init_db()
    yield


# ─── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="PM Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Pydantic Models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str


# ─── Helper: build status snapshot ────────────────────────────────────────────

async def get_status_snapshot() -> dict:
    processes = await database.get_all_processes()
    icm_stages = await database.get_all_icm_stages()

    # Check Ollama
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    ollama_status = "disconnected"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{ollama_host}/api/tags")
            if resp.status_code == 200:
                ollama_status = "connected"
    except Exception:
        pass

    # Check Claude
    claude_status = (
        "configured" if os.environ.get("ANTHROPIC_API_KEY") else "not_configured"
    )

    return {
        "processes": processes,
        "icm_stages": icm_stages,
        "ollama_status": ollama_status,
        "claude_status": claude_status,
    }


# ─── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return await get_status_snapshot()


@app.get("/api/reports/latest")
async def get_latest_report():
    report = await database.get_latest_report("daily_report")
    return {"report": report}


@app.get("/api/icm/stages")
async def get_icm_stages():
    stages = await database.get_all_icm_stages()
    return {"stages": stages}


@app.get("/api/chat/history")
async def get_chat_history():
    messages = await database.get_chat_history(limit=50)
    return {"messages": messages}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    processes = await database.get_all_processes()
    icm_stages = await database.get_all_icm_stages()
    latest_report = await database.get_latest_report("daily_report")

    response_text, action = await orchestrator.chat(
        user_message=request.message,
        process_states=processes,
        icm_stages=icm_stages,
        latest_report=latest_report,
    )

    if action:
        asyncio.create_task(_execute_action(action))

    return {"response": response_text, "action_triggered": action}


async def _execute_action(action: str) -> None:
    if action == "run_linear_report":
        process_id = str(uuid.uuid4())
        agent = LinearReportAgent()
        agent.set_broadcast(broadcast_update)
        try:
            await agent.run(process_id)
        except Exception as exc:
            print(f"[Action] Linear report error: {exc}")

    elif action.startswith("run_icm_stage:"):
        try:
            stage_number = int(action.split(":")[1])
            process_id = str(uuid.uuid4())
            agent = ICMRunnerAgent(stage_number)
            agent.set_broadcast(broadcast_update)
            await agent.run(process_id)
        except Exception as exc:
            print(f"[Action] ICM stage error: {exc}")


@app.post("/api/agents/linear-report/run")
async def run_linear_report():
    process_id = str(uuid.uuid4())

    await database.upsert_process(
        id=process_id,
        name="Linear Daily Report",
        type="linear_report",
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    agent = LinearReportAgent()
    agent.set_broadcast(broadcast_update)

    asyncio.create_task(_run_agent(agent, process_id))

    return {"process_id": process_id, "status": "started"}


@app.post("/api/icm/run/{stage_number}")
async def run_icm_stage(stage_number: int):
    if stage_number not in range(2, 7):
        raise HTTPException(status_code=400, detail="Stage number must be 2-6")

    process_id = str(uuid.uuid4())

    stage_names = {
        2: "discovery",
        3: "opportunity",
        4: "prd",
        5: "critique",
        6: "stories",
    }

    await database.upsert_process(
        id=process_id,
        name=f"ICM Stage {stage_number}: {stage_names[stage_number].title()}",
        type=f"icm_stage_{stage_number}",
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    agent = ICMRunnerAgent(stage_number)
    agent.set_broadcast(broadcast_update)

    asyncio.create_task(_run_agent(agent, process_id))

    return {"process_id": process_id, "stage": stage_number, "status": "started"}


async def _run_agent(agent, process_id: str) -> None:
    try:
        await agent.run(process_id)
    except Exception as exc:
        print(f"[Agent] Error running {type(agent).__name__}: {exc}")


# ─── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/updates")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current status snapshot on connect
        snapshot = await get_status_snapshot()
        await websocket.send_json({"type": "status_snapshot", **snapshot})

        # Keep alive
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.disconnect(websocket)


# ─── Static Files ──────────────────────────────────────────────────────────────

frontend_dir = Path(__file__).parent.parent / "frontend"

if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(frontend_dir / "index.html"))
