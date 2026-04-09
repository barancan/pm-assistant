import asyncio
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File
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

# Registry of running asyncio tasks keyed by process_id
_running_tasks: dict[str, asyncio.Task] = {}


async def broadcast_update(event_type: str, data: dict) -> None:
    await manager.broadcast({"type": event_type, **data})


# ─── Lifespan ──────────────────────────────────────────────────────────────────

_ZOMBIE_TIMEOUT_SECONDS = 10 * 60  # 10 minutes


async def _zombie_sweeper() -> None:
    """Background task: every 60 s, mark processes that have been 'running'
    for more than _ZOMBIE_TIMEOUT_SECONDS as 'error' and broadcast the update."""
    while True:
        await asyncio.sleep(60)
        try:
            processes = await database.get_all_processes()
            now = datetime.now(timezone.utc)
            for p in processes:
                if p["status"] != "running":
                    continue
                started = p.get("started_at")
                if not started:
                    continue
                age = (now - datetime.fromisoformat(started)).total_seconds()
                if age >= _ZOMBIE_TIMEOUT_SECONDS:
                    await database.upsert_process(
                        id=p["id"],
                        name=p["name"],
                        type=p["type"],
                        status="error",
                        completed_at=now.isoformat(),
                        error_message="Process timed out (running > 10 minutes)",
                    )
                    _running_tasks.pop(p["id"], None)
                    await broadcast_update("process_update", {
                        "process_id": p["id"],
                        "status": "error",
                        "error_message": "Process timed out (running > 10 minutes)",
                    })
                    print(f"[Sweeper] Marked zombie process {p['id'][:8]} ({p['name']}) as error")
        except Exception as exc:
            print(f"[Sweeper] Error: {exc}")


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

    # Start background zombie sweeper
    sweeper = asyncio.create_task(_zombie_sweeper())
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass


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


class FileWriteRequest(BaseModel):
    path: str
    content: str


class PromoteRequest(BaseModel):
    path: str


class ICMPromoteRequest(BaseModel):
    selected_files: list[str]
    source: str = "previous_stage"  # "previous_stage" | "intake_trusted"


# ─── ICM stage name helpers ────────────────────────────────────────────────────

_STAGE_NAMES = {
    2: "discovery",
    3: "opportunity",
    4: "prd",
    5: "critique",
    6: "stories",
}

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_UPLOAD_ALLOWED_EXT = {".md", ".txt", ".jpg", ".jpeg", ".png"}


def _sanitize_filename(name: str) -> str:
    """Strip path separators, replace spaces with underscores, keep safe chars."""
    name = Path(name).name  # drop any directory component
    name = name.replace(" ", "_")
    name = re.sub(r"[^\w\-.]", "", name)
    return name or "upload"


def _stage_path(stage_number: int) -> str:
    return f"0{stage_number}_{_STAGE_NAMES[stage_number]}"


def _list_md_files(directory: Path) -> list[dict]:
    """Return .md files in a directory, sorted newest-first."""
    if not directory.is_dir():
        return []
    files = []
    for f in directory.iterdir():
        if f.name.startswith(".") or f.name == ".gitkeep" or not f.is_file():
            continue
        if f.suffix.lower() != ".md":
            continue
        stat = f.stat()
        files.append({
            "name": f.name,
            "path": str(f.relative_to(WORKSPACE_PATH)),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return sorted(files, key=lambda x: x["modified_at"], reverse=True)


# ─── Workspace path helpers ────────────────────────────────────────────────────

_WRITABLE_PATTERNS = [
    "01_intake/quarantine",
    "01_intake/trusted",
    "02_discovery/output",
    "03_opportunity/output",
    "04_prd/output",
    "05_critique/output",
    "06_stories/output",
]


def _resolve_ws_path(relative: str) -> Path:
    """Resolve a relative path against WORKSPACE_PATH; raise 400/403 if invalid."""
    if not relative or Path(relative).is_absolute():
        raise HTTPException(status_code=400, detail="Path must be relative")
    resolved = (WORKSPACE_PATH / relative).resolve()
    if not str(resolved).startswith(str(WORKSPACE_PATH)):
        raise HTTPException(status_code=403, detail="Path escapes workspace boundary")
    return resolved


def _assert_writable_ws(resolved: Path) -> None:
    relative = str(resolved.relative_to(WORKSPACE_PATH))
    if not any(p in relative for p in _WRITABLE_PATTERNS):
        raise HTTPException(status_code=403, detail="Path is not in an approved write location")


# ─── Helper: build status snapshot ────────────────────────────────────────────

async def get_status_snapshot() -> dict:
    processes = await database.get_all_processes()
    raw_stages = await database.get_all_icm_stages()
    icm_stages = []
    for stage in raw_stages:
        n = stage["stage_number"]
        if n == 1:
            input_dir = WORKSPACE_PATH / "01_intake" / "quarantine"
            output_dir = WORKSPACE_PATH / "01_intake" / "trusted"
        else:
            stage_dir = WORKSPACE_PATH / _stage_path(n)
            input_dir = stage_dir / "input"
            output_dir = stage_dir / "output"
        icm_stages.append({
            **stage,
            "input_file_count": _count_files(input_dir),
            "output_file_count": _count_files(output_dir),
        })

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

    # Check Linear
    linear_status = (
        "configured" if os.environ.get("LINEAR_API_KEY") else "not_configured"
    )

    return {
        "processes": processes,
        "icm_stages": icm_stages,
        "ollama_status": ollama_status,
        "claude_status": claude_status,
        "linear_status": linear_status,
    }


# ─── REST Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/status")
async def get_status():
    return await get_status_snapshot()


# ─── Settings Endpoints ────────────────────────────────────────────────────────

_ENV_LOCAL_PATH = _root / ".env.local"

_SETTINGS_KEYS = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "linear_api_key": "LINEAR_API_KEY",
    "ollama_host": "OLLAMA_HOST",
    "ollama_model": "OLLAMA_MODEL",
}

_STAGE_MODEL_KEYS = {2: "STAGE_2_MODEL", 3: "STAGE_3_MODEL", 6: "STAGE_6_MODEL"}


def _mask(value: Optional[str]) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "****..." + value[-4:]


def _read_env_local() -> dict:
    """Parse .env.local into a key→value dict."""
    result = {}
    if not _ENV_LOCAL_PATH.exists():
        return result
    for line in _ENV_LOCAL_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result


def _write_env_local(pairs: dict) -> None:
    """Update .env.local with given key=value pairs. Only modifies listed keys."""
    existing = _read_env_local()
    existing.update(pairs)
    lines = [f"{k}={v}" for k, v in existing.items() if v != ""]
    _ENV_LOCAL_PATH.write_text("\n".join(lines) + "\n")


@app.get("/api/settings")
async def get_settings():
    stage_models = {
        str(stage): os.environ.get(env_key, os.environ.get("OLLAMA_MODEL", ""))
        for stage, env_key in _STAGE_MODEL_KEYS.items()
    }
    return {
        "anthropic_api_key": _mask(os.environ.get("ANTHROPIC_API_KEY")),
        "linear_api_key": _mask(os.environ.get("LINEAR_API_KEY")),
        "ollama_host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        "ollama_model": os.environ.get("OLLAMA_MODEL", ""),
        "stage_models": stage_models,
    }


class SettingsRequest(BaseModel):
    anthropic_api_key: Optional[str] = None
    linear_api_key: Optional[str] = None
    ollama_host: Optional[str] = None
    ollama_model: Optional[str] = None
    stage_models: Optional[dict] = None  # {"2": "gemma3:4b", ...}


@app.post("/api/settings")
async def save_settings(req: SettingsRequest):
    updates: dict = {}
    field_map = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "linear_api_key": "LINEAR_API_KEY",
        "ollama_host": "OLLAMA_HOST",
        "ollama_model": "OLLAMA_MODEL",
    }
    data = req.model_dump(exclude_none=True)
    for field, env_key in field_map.items():
        if field in data and data[field] not in (None, ""):
            updates[env_key] = data[field]
            os.environ[env_key] = data[field]

    if req.stage_models:
        for stage_str, model in req.stage_models.items():
            env_key = _STAGE_MODEL_KEYS.get(int(stage_str))
            if env_key and model:
                updates[env_key] = model
                os.environ[env_key] = model

    if updates:
        _write_env_local(updates)

    return await get_settings()


@app.post("/api/settings/test/{service}")
async def test_service(service: str):
    if service == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            return {"ok": False, "message": "ANTHROPIC_API_KEY not set"}
        try:
            import anthropic as _anthropic
            client = _anthropic.AsyncAnthropic(api_key=key)
            await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return {"ok": True, "message": "Connected"}
        except Exception as e:
            return {"ok": False, "message": str(e)[:120]}

    elif service == "linear":
        key = os.environ.get("LINEAR_API_KEY")
        if not key:
            return {"ok": False, "message": "LINEAR_API_KEY not set"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    "https://api.linear.app/graphql",
                    json={"query": "{ viewer { id name } }"},
                    headers={"Authorization": key, "Content-Type": "application/json"},
                )
                data = resp.json()
                if "errors" in data:
                    return {"ok": False, "message": data["errors"][0].get("message", "API error")}
                name = data.get("data", {}).get("viewer", {}).get("name", "")
                return {"ok": True, "message": f"Connected as {name}"}
        except Exception as e:
            return {"ok": False, "message": str(e)[:120]}

    elif service == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{host}/api/tags")
                if resp.status_code == 200:
                    count = len(resp.json().get("models", []))
                    return {"ok": True, "message": f"Connected — {count} model(s) available"}
                return {"ok": False, "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "message": str(e)[:120]}

    raise HTTPException(status_code=400, detail="Unknown service")


@app.get("/api/settings/ollama-models")
async def get_ollama_models():
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{host}/api/tags")
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                return {"models": models}
    except Exception:
        pass
    return {"models": []}


@app.get("/api/reports/latest")
async def get_latest_report():
    report = await database.get_latest_report("daily_report")
    return {"report": report}


@app.get("/api/reports/history")
async def get_report_history(type: str = "daily_report", limit: int = 20):
    history = await database.get_report_history(type, limit)
    return {"reports": history}


@app.get("/api/reports/{report_id}")
async def get_report_by_id(report_id: str):
    report = await database.get_report_by_id(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"report": report}


def _count_files(directory: Path) -> int:
    """Count non-hidden, non-gitkeep files in a directory (any extension)."""
    if not directory.is_dir():
        return 0
    return sum(
        1 for f in directory.iterdir()
        if f.is_file() and not f.name.startswith(".") and f.name != ".gitkeep"
    )


@app.get("/api/icm/stages")
async def get_icm_stages():
    stages = await database.get_all_icm_stages()
    enriched = []
    for stage in stages:
        n = stage["stage_number"]
        if n == 1:
            input_dir = WORKSPACE_PATH / "01_intake" / "quarantine"
            output_dir = WORKSPACE_PATH / "01_intake" / "trusted"
        else:
            stage_dir = WORKSPACE_PATH / _stage_path(n)
            input_dir = stage_dir / "input"
            output_dir = stage_dir / "output"
        enriched.append({
            **stage,
            "input_file_count": _count_files(input_dir),
            "output_file_count": _count_files(output_dir),
        })
    return {"stages": enriched}


@app.get("/api/workspace/files")
async def list_workspace_files(dir: str):
    resolved = _resolve_ws_path(dir)
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    files = []
    for f in sorted(resolved.iterdir()):
        if f.name.startswith(".") or f.is_dir():
            continue
        stat = f.stat()
        files.append({
            "name": f.name,
            "path": str(f.relative_to(WORKSPACE_PATH)),
            "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return {"files": files}


@app.get("/api/workspace/file")
async def read_workspace_file(path: str):
    resolved = _resolve_ws_path(path)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not valid UTF-8 text")
    return {"path": path, "content": content}


@app.get("/api/workspace/file-raw")
async def read_workspace_file_raw(path: str):
    """Serve a workspace file as raw bytes — used for image previews."""
    resolved = _resolve_ws_path(path)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(resolved))


@app.put("/api/workspace/file")
async def write_workspace_file(req: FileWriteRequest):
    resolved = _resolve_ws_path(req.path)
    _assert_writable_ws(resolved)
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found — only editing existing files is supported")
    resolved.write_text(req.content, encoding="utf-8")
    return {"path": req.path, "saved": True}


@app.post("/api/workspace/promote")
async def promote_file(req: PromoteRequest):
    resolved = _resolve_ws_path(req.path)
    relative = str(resolved.relative_to(WORKSPACE_PATH))
    if "01_intake/quarantine" not in relative:
        raise HTTPException(status_code=403, detail="Can only promote files from 01_intake/quarantine/")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    dest = WORKSPACE_PATH / "01_intake" / "trusted" / resolved.name
    resolved.rename(dest)
    return {"promoted_to": str(dest.relative_to(WORKSPACE_PATH))}


@app.get("/api/icm/{stage_number}/output-files")
async def get_stage_output_files(stage_number: int):
    if stage_number not in range(1, 7):
        raise HTTPException(status_code=400, detail="Stage number must be 1-6")
    # Stage 1 (intake) reads from trusted/ instead of output/
    if stage_number == 1:
        out_dir = WORKSPACE_PATH / "01_intake" / "trusted"
    else:
        out_dir = WORKSPACE_PATH / _stage_path(stage_number) / "output"
    return {"files": _list_md_files(out_dir)}


@app.post("/api/icm/stages/{stage_number}/done")
async def mark_stage_done(stage_number: int):
    if stage_number not in range(2, 7):
        raise HTTPException(status_code=400, detail="Stage must be 2-6")
    await database.update_icm_stage(stage_number, "done")
    return {"stage_number": stage_number, "status": "done"}


@app.post("/api/icm/stages/{stage_number}/retry")
async def retry_icm_stage(stage_number: int):
    """Re-run a stage that errored. Input files are left untouched."""
    if stage_number not in range(2, 7):
        raise HTTPException(status_code=400, detail="Stage must be 2-6")
    # Delegate to the standard run endpoint logic
    return await run_icm_stage(stage_number)


@app.get("/api/icm/{stage_number}/input-sources")
async def get_stage_input_sources(stage_number: int):
    if stage_number not in range(2, 7):
        raise HTTPException(status_code=400, detail="Stage must be 2-6")

    # Source A: previous stage output (or intake/trusted for stage 2)
    if stage_number == 2:
        prev_dir = WORKSPACE_PATH / "01_intake" / "trusted"
        previous_stage = {"stage_number": 1, "stage_name": "intake", "files": _list_md_files(prev_dir)}
    else:
        prev_num = stage_number - 1
        prev_dir = WORKSPACE_PATH / _stage_path(prev_num) / "output"
        previous_stage = {"stage_number": prev_num, "stage_name": _STAGE_NAMES[prev_num], "files": _list_md_files(prev_dir)}

    # Source B: intake trusted (always available)
    trusted_dir = WORKSPACE_PATH / "01_intake" / "trusted"
    intake_trusted = {"files": _list_md_files(trusted_dir)}

    # Source C: already queued in this stage's input
    input_dir = WORKSPACE_PATH / _stage_path(stage_number) / "input"
    current_input = {"files": _list_md_files(input_dir)}

    return {
        "stage_number": stage_number,
        "previous_stage": previous_stage,
        "intake_trusted": intake_trusted,
        "current_input": current_input,
    }


@app.post("/api/icm/promote/{stage_number}")
async def promote_icm_files(stage_number: int, req: ICMPromoteRequest):
    if stage_number not in range(1, 6):
        raise HTTPException(status_code=400, detail="Stage number must be 1-5 (stage 6 has no next stage)")
    if not req.selected_files:
        raise HTTPException(status_code=400, detail="selected_files must be non-empty")

    # Resolve source directory based on `source` field (automation-compatible)
    if req.source == "intake_trusted" or stage_number == 1:
        src_dir = WORKSPACE_PATH / "01_intake" / "trusted"
    elif stage_number == 2:
        # Stage 2's "previous_stage" is intake/trusted (no stage 1 output/ exists)
        src_dir = WORKSPACE_PATH / "01_intake" / "trusted"
    else:
        src_dir = WORKSPACE_PATH / _stage_path(stage_number) / "output"
    next_stage = stage_number + 1
    dest_dir = WORKSPACE_PATH / _stage_path(next_stage) / "input"

    promoted = []
    for filename in req.selected_files:
        safe_name = _sanitize_filename(filename)
        src = (src_dir / safe_name).resolve()
        # Escape check
        if not str(src).startswith(str(WORKSPACE_PATH)):
            raise HTTPException(status_code=403, detail=f"Invalid path: {filename}")
        if not src.is_file():
            raise HTTPException(status_code=404, detail=f"File not found: {filename}")
        if src.suffix.lower() != ".md":
            raise HTTPException(status_code=400, detail=f"Only .md files can be promoted: {filename}")
        dest = (dest_dir / safe_name).resolve()
        if not str(dest).startswith(str(WORKSPACE_PATH)):
            raise HTTPException(status_code=403, detail="Destination path escapes workspace")
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        promoted.append(safe_name)

    next_path = f"{_stage_path(next_stage)}/input"
    await broadcast_update("process_update", {
        "type": "icm_promote",
        "from_stage": stage_number,
        "to_stage": next_stage,
        "files": promoted,
    })
    return {"promoted": promoted, "to": next_path, "count": len(promoted)}


@app.post("/api/icm/{stage_number}/upload")
async def upload_to_stage_input(stage_number: int, file: UploadFile = File(...)):
    if stage_number not in range(2, 7):
        raise HTTPException(status_code=400, detail="Stage number must be 2-6")

    original_name = file.filename or "upload"
    safe_name = _sanitize_filename(original_name)
    ext = Path(safe_name).suffix.lower()

    if ext not in _UPLOAD_ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Only .md, .txt, .jpg, .jpeg, .png files accepted")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")

    dest_dir = WORKSPACE_PATH / _stage_path(stage_number) / "input"
    dest_dir.mkdir(parents=True, exist_ok=True)

    if ext == ".txt":
        final_name = Path(safe_name).stem + ".md"
        dest = (dest_dir / final_name).resolve()
        if not str(dest).startswith(str(WORKSPACE_PATH)):
            raise HTTPException(status_code=403, detail="Path escapes workspace")
        dest.write_text(content.decode("utf-8", errors="replace"), encoding="utf-8")
        file_type = "markdown"
    elif ext == ".md":
        final_name = safe_name
        dest = (dest_dir / final_name).resolve()
        if not str(dest).startswith(str(WORKSPACE_PATH)):
            raise HTTPException(status_code=403, detail="Path escapes workspace")
        dest.write_text(content.decode("utf-8", errors="replace"), encoding="utf-8")
        file_type = "markdown"
    else:  # image
        final_name = safe_name
        dest = (dest_dir / final_name).resolve()
        if not str(dest).startswith(str(WORKSPACE_PATH)):
            raise HTTPException(status_code=403, detail="Path escapes workspace")
        dest.write_bytes(content)
        file_type = "image"

    return {
        "saved_as": final_name,
        "stage_input": f"{_stage_path(stage_number)}/input/",
        "type": file_type,
    }


@app.get("/api/chat/history")
async def get_chat_history():
    messages = await database.get_chat_history(limit=50)
    return {"messages": messages}


@app.delete("/api/chat/history")
async def clear_chat_history():
    await database.clear_chat_history()
    return {"ok": True}


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
        task = asyncio.create_task(_run_agent(agent, process_id))
        _running_tasks[process_id] = task

    elif action.startswith("run_icm_stage:"):
        try:
            stage_number = int(action.split(":")[1])
            process_id = str(uuid.uuid4())
            agent = ICMRunnerAgent(stage_number)
            agent.set_broadcast(broadcast_update)
            task = asyncio.create_task(_run_agent(agent, process_id))
            _running_tasks[process_id] = task
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

    task = asyncio.create_task(_run_agent(agent, process_id))
    _running_tasks[process_id] = task

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

    task = asyncio.create_task(_run_agent(agent, process_id))
    _running_tasks[process_id] = task

    return {"process_id": process_id, "stage": stage_number, "status": "started"}


@app.post("/api/processes/{process_id}/cancel")
async def cancel_process(process_id: str):
    task = _running_tasks.get(process_id)
    if task and not task.done():
        # Live task — cancel it; _run_agent's CancelledError handler updates the DB
        task.cancel()
        return {"process_id": process_id, "status": "cancelling"}

    # No live task — could be a stale 'running' record from before this server start.
    # Mark it cancelled in the DB directly.
    processes = await database.get_all_processes()
    match = next((p for p in processes if p["id"] == process_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Process not found")
    if match["status"] != "running":
        raise HTTPException(status_code=400, detail=f"Process is already {match['status']}")
    await database.upsert_process(
        id=process_id,
        name=match["name"],
        type=match["type"],
        status="cancelled",
        completed_at=datetime.now(timezone.utc).isoformat(),
        error_message="Cancelled by user (process had no live task)",
    )
    await broadcast_update("process_update", {"process_id": process_id, "status": "cancelled"})
    return {"process_id": process_id, "status": "cancelled"}


async def _run_agent(agent, process_id: str) -> None:
    try:
        await agent.run(process_id)
    except asyncio.CancelledError:
        await database.upsert_process(
            id=process_id,
            name=getattr(agent, "_process_name", "Process"),
            type=getattr(agent, "_process_type", "agent"),
            status="cancelled",
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_message="Cancelled by user",
        )
        await broadcast_update("process_update", {
            "process_id": process_id,
            "status": "cancelled",
            "name": getattr(agent, "_process_name", "Process"),
        })
    except Exception as exc:
        print(f"[Agent] Error running {type(agent).__name__}: {exc}")
    finally:
        _running_tasks.pop(process_id, None)


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
