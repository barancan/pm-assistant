import json
import os
from typing import Optional

import anthropic

import database


class Orchestrator:
    SYSTEM_PROMPT = """You are the PM Assistant orchestrator a senior product manager.
You have awareness of all running processes, ICM pipeline state, and the latest daily report.

You can help with:
- Explaining what agents are doing or have done
- Recommending which ICM stage to run next
- Summarising reports and findings
- Helping prioritise based on the daily brief
- Answering questions about the PM workflow

You CANNOT:
- Auto-approve or auto-advance ICM stages (always requires user command)
- Modify Linear without explicit user instruction
- Access files outside the workspace

When the user gives a command like "run the daily report" or "run stage 3", respond confirming you're triggering it, then the system will execute it. Use the context provided to give informed, specific advice. Be direct. No corporate speak."""

    async def chat(
        self,
        user_message: str,
        process_states: list[dict],
        icm_stages: list[dict],
        latest_report: Optional[dict],
    ) -> tuple[str, Optional[str]]:
        """
        Returns (response_text, action_command | None)
        action_command is one of:
          "run_linear_report"
          "run_icm_stage:{stage_number}"
          None
        """
        # Build context block
        report_summary = "No report yet"
        if latest_report:
            content = latest_report.get("content", {})
            if isinstance(content, dict):
                report_summary = content.get("summary", "No summary available")
            else:
                report_summary = str(content)[:300]

        context = f"""=== CURRENT SYSTEM STATE ===

Active Processes:
{json.dumps(process_states, indent=2)}

ICM Pipeline Stages:
{json.dumps(icm_stages, indent=2)}

Latest Daily Report Summary:
{report_summary}

=== END STATE ==="""

        # Detect action intent from user message
        action = None
        msg_lower = user_message.lower()
        if any(
            x in msg_lower
            for x in [
                "run report",
                "daily report",
                "linear report",
                "generate report",
                "fetch linear",
            ]
        ):
            action = "run_linear_report"
        else:
            stage_names = {
                2: "discovery",
                3: "opportunity",
                4: "prd",
                5: "critique",
                6: "stories",
            }
            for i in range(2, 7):
                if f"stage {i}" in msg_lower or f"run {stage_names[i]}" in msg_lower:
                    action = f"run_icm_stage:{i}"
                    break

        # Get chat history for continuity
        history = await database.get_chat_history(limit=20)

        # Build messages list
        messages = []
        for msg in history[-10:]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append(
            {
                "role": "user",
                "content": f"{context}\n\nUser: {user_message}",
            }
        )

        # Call Claude
        client = anthropic.AsyncAnthropic(
            api_key=os.environ.get("ANTHROPIC_API_KEY")
        )
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=self.SYSTEM_PROMPT,
            messages=messages,
        )

        response_text = response.content[0].text

        # Save both messages to db
        await database.save_chat_message("user", user_message)
        await database.save_chat_message("assistant", response_text)

        return response_text, action
