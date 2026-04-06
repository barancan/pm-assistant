import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import database
from .base_agent import BaseAgent


class ICMRunnerAgent(BaseAgent):
    STAGE_MODEL_MAP = {
        2: "ollama",
        3: "ollama",
        4: "claude",
        5: "claude",
        6: "ollama",
    }

    STAGE_NAMES = {
        2: "discovery",
        3: "opportunity",
        4: "prd",
        5: "critique",
        6: "stories",
    }

    def __init__(self, stage_number: int):
        if stage_number not in range(2, 7):
            raise ValueError(f"Invalid stage number: {stage_number}. Must be 2-6.")

        self.stage_number = stage_number
        self.stage_name = self.STAGE_NAMES[stage_number]
        self.stage_path = f"0{stage_number}_{self.stage_name}"
        self.AGENT_ALLOWED_WRITES = [f"{self.stage_path}/output"]

        self._process_name = f"ICM Stage {stage_number}: {self.stage_name.title()}"
        self._process_type = f"icm_stage_{stage_number}"

    async def run(self, process_id: str) -> str:
        # Step 1 — Validate stage and update status
        if self.stage_number not in range(2, 7):
            raise ValueError(f"Invalid stage number: {self.stage_number}. Must be 2-6.")

        await database.upsert_process(
            id=process_id,
            name=self._process_name,
            type=self._process_type,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        await database.update_icm_stage(self.stage_number, "running")
        await self.broadcast(
            "process_update",
            {
                "process_id": process_id,
                "status": "running",
                "stage": self.stage_name,
                "stage_number": self.stage_number,
            },
        )

        try:
            # Step 2 — Read CONTEXT.md for this stage
            context_path = f"{self.stage_path}/CONTEXT.md"
            system_prompt = self.read_file(context_path)

            # Step 3 — Collect input files
            input_dir = f"{self.stage_path}/input"
            input_files = self.read_directory(input_dir)
            md_files = [f for f in input_files if f.endswith(".md")]

            if not md_files:
                warning_msg = (
                    f"No input files found. "
                    f"Add files to {self.stage_path}/input/ and retry."
                )
                await self.broadcast(
                    "process_update",
                    {
                        "process_id": process_id,
                        "status": "done",
                        "message": warning_msg,
                    },
                )
                await database.upsert_process(
                    id=process_id,
                    name=self._process_name,
                    type=self._process_type,
                    status="done",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    output_summary=warning_msg,
                )
                await database.update_icm_stage(self.stage_number, "idle")
                return warning_msg

            combined_input = ""
            for filename in md_files:
                file_path = f"{self.stage_path}/input/{filename}"
                content = self.read_file(file_path)
                combined_input += f"=== FILE: {filename} ===\n{content}\n\n"

            # Step 4 — Call appropriate model with streaming
            model_type = self.STAGE_MODEL_MAP[self.stage_number]
            full_response = ""

            async def token_callback(token: str):
                nonlocal full_response
                full_response += token
                await self.broadcast(
                    "token_stream",
                    {
                        "stage": self.stage_name,
                        "stage_number": self.stage_number,
                        "token": token,
                    },
                )

            if model_type == "claude":
                await self.call_claude(system_prompt, combined_input, stream_callback=token_callback)
            else:
                await self.call_ollama(system_prompt, combined_input, stream_callback=token_callback)

            # Step 5 — Write output
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            output_filename = f"draft_{timestamp}.md"
            output_path = f"{self.stage_path}/output/{output_filename}"

            header = (
                f"# {self.stage_name.title()} Output\n"
                f"Generated: {datetime.now(timezone.utc).isoformat()}\n\n"
            )
            self.write_file(output_path, header + full_response)

            # Step 6 — Update stage status to needs_review
            full_output_path = str(self.WORKSPACE / output_path)
            await database.update_icm_stage(
                self.stage_number, "needs_review", output_path=full_output_path
            )

            await database.upsert_process(
                id=process_id,
                name=self._process_name,
                type=self._process_type,
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                output_summary=f"Output written to {output_path}",
            )

            await self.broadcast(
                "process_update",
                {
                    "process_id": process_id,
                    "status": "done",
                    "stage": self.stage_name,
                    "stage_number": self.stage_number,
                    "output_path": output_path,
                },
            )

            return full_output_path

        except Exception as exc:
            await database.upsert_process(
                id=process_id,
                name=self._process_name,
                type=self._process_type,
                status="error",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_message=str(exc),
            )
            await database.update_icm_stage(self.stage_number, "error")
            await self.broadcast(
                "error",
                {"process_id": process_id, "stage": self.stage_name, "message": str(exc)},
            )
            raise
