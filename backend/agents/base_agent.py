import asyncio
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
import anthropic

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import database


class BaseAgent(ABC):
    WORKSPACE: Path = Path(
        os.environ.get("WORKSPACE_PATH", "./workspace")
    ).resolve()

    READONLY_PATTERNS = [
        "CLAUDE.md",
        "CONTEXT.md",
        "_core",
        "_config",
    ]

    WRITABLE_PATTERNS = [
        "01_intake/quarantine",
        "01_intake/trusted",
        "02_discovery/output",
        "03_opportunity/output",
        "04_prd/output",
        "05_critique/output",
        "06_stories/output",
    ]

    AGENT_ALLOWED_WRITES: list[str] = []

    _broadcast_fn: Optional[Callable] = None

    def _validate_read(self, path: Path) -> Path:
        resolved = path.resolve()
        if not str(resolved).startswith(str(self.WORKSPACE)):
            raise PermissionError(
                f"Security: path escapes workspace boundary: {path}"
            )
        return resolved

    def _validate_write(self, path: Path) -> Path:
        resolved = path.resolve()
        if not str(resolved).startswith(str(self.WORKSPACE)):
            raise PermissionError(
                f"Security: path escapes workspace boundary: {path}"
            )
        relative = str(resolved.relative_to(self.WORKSPACE))

        for pattern in self.READONLY_PATTERNS:
            if pattern in relative:
                raise PermissionError(
                    f"Security: write blocked to protected path: {relative}"
                )

        if not any(pattern in relative for pattern in self.WRITABLE_PATTERNS):
            raise PermissionError(
                f"Security: path not in approved write locations: {relative}"
            )

        if self.AGENT_ALLOWED_WRITES and not any(
            pattern in relative for pattern in self.AGENT_ALLOWED_WRITES
        ):
            raise PermissionError(
                f"Security: path not in agent's allowed write locations: {relative}"
            )

        return resolved

    def read_file(self, path: str) -> str:
        full_path = self.WORKSPACE / path
        validated = self._validate_read(full_path)
        return validated.read_text(encoding="utf-8")

    def read_directory(self, path: str) -> list[str]:
        full_path = self.WORKSPACE / path
        validated = self._validate_read(full_path)
        if not validated.is_dir():
            return []
        return [f.name for f in validated.iterdir() if not f.name.startswith(".")]

    def write_file(self, path: str, content: str) -> None:
        full_path = self.WORKSPACE / path
        validated = self._validate_write(full_path)
        validated.parent.mkdir(parents=True, exist_ok=True)
        validated.write_text(content, encoding="utf-8")

    async def call_ollama(
        self,
        system: str,
        user: str,
        stream_callback: Optional[Callable] = None,
    ) -> str:
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
        url = f"{ollama_host}/api/chat"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": stream_callback is not None,
        }

        full_response = ""

        async with httpx.AsyncClient(timeout=300.0) as client:
            if stream_callback is not None:
                async with client.stream("POST", url, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                full_response += token
                                await stream_callback(token)
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
            else:
                response = await client.post(url, json={**payload, "stream": False})
                response.raise_for_status()
                data = response.json()
                full_response = data.get("message", {}).get("content", "")

        return full_response

    async def call_claude(
        self,
        system: str,
        user: str,
        stream_callback: Optional[Callable] = None,
    ) -> str:
        client = anthropic.AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )
        full_response = ""

        if stream_callback is not None:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            ) as stream:
                async for text in stream.text_stream:
                    full_response += text
                    await stream_callback(text)
        else:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            full_response = response.content[0].text

        return full_response

    async def update_process_status(
        self,
        process_id: str,
        status: str,
        summary: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        completed_at = None
        if status in ("done", "error"):
            completed_at = datetime.now(timezone.utc).isoformat()

        await database.upsert_process(
            id=process_id,
            name=getattr(self, "_process_name", "Unknown"),
            type=getattr(self, "_process_type", "agent"),
            status=status,
            completed_at=completed_at,
            output_summary=summary,
            error_message=error,
        )

    def set_broadcast(self, fn: Callable) -> None:
        self._broadcast_fn = fn

    async def broadcast(self, event_type: str, data: dict) -> None:
        if self._broadcast_fn is not None:
            try:
                await self._broadcast_fn(event_type, data)
            except Exception as exc:
                print(f"[BaseAgent] Broadcast error: {exc}")
        else:
            print(f"[BaseAgent] {event_type}: {data}")

    @abstractmethod
    async def run(self, process_id: str):
        pass
