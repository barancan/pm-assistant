import os
from datetime import datetime, timezone

import httpx

from .base_agent import BaseAgent
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database


class LinearReportAgent(BaseAgent):
    AGENT_ALLOWED_WRITES = ["01_intake/trusted"]

    _process_name = "Linear Daily Report"
    _process_type = "linear_report"

    _GRAPHQL_URL = "https://api.linear.app/graphql"

    async def _graphql(
        self, client: httpx.AsyncClient, query: str, variables: dict | None = None
    ) -> dict:
        linear_api_key = os.environ.get("LINEAR_API_KEY", "")
        headers = {
            "Content-Type": "application/json",
            "Authorization": linear_api_key,  # Linear accepts the key without "Bearer"
        }
        payload: dict = {"query": query}
        if variables:
            payload["variables"] = variables
        try:
            response = await client.post(
                self._GRAPHQL_URL, json=payload, headers=headers, timeout=30.0
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            return {"errors": [{"message": str(exc)}]}

    async def run(self, process_id: str) -> dict:
        # Step 1 — Update status to running
        await database.upsert_process(
            id=process_id,
            name=self._process_name,
            type=self._process_type,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        await self.broadcast(
            "process_update",
            {
                "process_id": process_id,
                "status": "running",
                "name": self._process_name,
            },
        )

        try:
            # Step 2 — Fetch Linear data via GraphQL
            from datetime import timedelta
            yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

            linear_data: dict = {}

            async with httpx.AsyncClient(timeout=30.0) as client:
                # Active projects
                projects_resp = await self._graphql(client, """
                    query { projects(first: 20) { nodes {
                        id name state { name } description
                    } } }
                """)
                linear_data["projects"] = self._gql_nodes(projects_resp, "projects")

                # Active issues (started / unstarted)
                active_resp = await self._graphql(client, """
                    query($filter: IssueFilter) { issues(first: 30, filter: $filter) { nodes {
                        id title priority state { name } assignee { name } updatedAt
                    } } }
                """, {"filter": {"state": {"type": {"in": ["started", "unstarted"]}}}})
                linear_data["active_issues"] = self._gql_nodes(active_resp, "issues")

                # Urgent / high priority issues (priority 1 = urgent)
                priority_resp = await self._graphql(client, """
                    query($filter: IssueFilter) { issues(first: 20, filter: $filter) { nodes {
                        id title priority state { name } assignee { name }
                    } } }
                """, {"filter": {"priority": {"lte": 2}}})
                linear_data["priority_issues"] = self._gql_nodes(priority_resp, "issues")

                # In-progress issues
                in_progress_resp = await self._graphql(client, """
                    query($filter: IssueFilter) { issues(first: 20, filter: $filter) { nodes {
                        id title assignee { name } state { name }
                    } } }
                """, {"filter": {"state": {"name": {"eq": "In Progress"}}}})
                linear_data["in_progress"] = self._gql_nodes(in_progress_resp, "issues")

                # Recent movement (last 24h)
                recent_resp = await self._graphql(client, """
                    query($filter: IssueFilter) { issues(first: 20, filter: $filter) { nodes {
                        id title updatedAt state { name }
                    } } }
                """, {"filter": {"updatedAt": {"gte": yesterday}}})
                linear_data["recent_movement"] = self._gql_nodes(recent_resp, "issues")

            await self.broadcast(
                "process_update",
                {
                    "process_id": process_id,
                    "status": "running",
                    "message": "Fetched Linear data, synthesizing...",
                },
            )

            # Step 3 — Synthesize with Claude API
            system_prompt = (
                "You are a senior PM assistant. Your job is to create a clear, "
                "actionable daily brief from Linear project data. Be direct and "
                "specific. No fluff. Focus on what moves the needle today."
            )

            formatted_data = self._format_linear_data(linear_data)

            user_message = f"""Here is the current Linear project data. Create a daily brief in exactly this format:

## Today's Priorities
[3 items max, ranked by impact. Each: Issue title, why it matters today, one specific next action]

## Blocked Items
[List any blocked issues with: what's blocked, what's blocking it, who needs to act]

## Momentum
[What moved forward in the last 24h. Keep brief.]

## Recommended Actions
[2-3 specific recommendations for today. Actionable, not vague.]

---
LINEAR DATA:
{formatted_data}
"""

            report_content = ""

            async def token_callback(token: str):
                nonlocal report_content
                report_content += token
                await self.broadcast(
                    "token_stream",
                    {"stage": "linear_report", "token": token},
                )

            await self.call_claude(system_prompt, user_message, stream_callback=token_callback)

            # Step 4 — Save results
            report_dict = {
                "raw_linear_data": linear_data,
                "report_markdown": report_content,
                "summary": report_content[:200],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

            report_id = await database.save_report("daily_report", report_dict)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            markdown_content = f"# Daily Report\nGenerated: {timestamp}\n\n{report_content}"
            self.write_file("01_intake/trusted/daily_report.md", markdown_content)

            await database.upsert_process(
                id=process_id,
                name=self._process_name,
                type=self._process_type,
                status="done",
                completed_at=datetime.now(timezone.utc).isoformat(),
                output_summary=report_content[:200],
            )

            await self.broadcast(
                "report_complete",
                {"report_id": report_id, "type": "daily_report"},
            )

            return report_dict

        except Exception as exc:
            await database.upsert_process(
                id=process_id,
                name=self._process_name,
                type=self._process_type,
                status="error",
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_message=str(exc),
            )
            await self.broadcast(
                "error",
                {"process_id": process_id, "message": str(exc)},
            )
            raise

    def _gql_nodes(self, response: dict, key: str) -> list:
        """Extract nodes list from a GraphQL response, silently returning [] on error."""
        if "errors" in response:
            err = response["errors"][0].get("message", "unknown") if response["errors"] else "unknown"
            print(f"[LinearReport] GraphQL error ({key}): {err}")
            return []
        data = response.get("data", {})
        collection = data.get(key, {})
        if isinstance(collection, dict):
            return collection.get("nodes", [])
        if isinstance(collection, list):
            return collection
        return []

    def _format_linear_data(self, data: dict) -> str:
        sections = []

        projects = data.get("projects", [])
        if projects:
            sections.append(f"ACTIVE PROJECTS ({len(projects)}):")
            for p in projects[:10]:
                if isinstance(p, dict):
                    name = p.get("name", "Unknown")
                    state_raw = p.get("state", {})
                    state = state_raw.get("name", "") if isinstance(state_raw, dict) else str(state_raw)
                    sections.append(f"  - {name} [{state}]")
        else:
            sections.append("ACTIVE PROJECTS: None fetched (Linear may require API key)")

        active = data.get("active_issues", [])
        sections.append(f"\nACTIVE ISSUES ({len(active)}):")
        for issue in active[:15]:
            if isinstance(issue, dict):
                title = issue.get("title", "Untitled")
                assignee = issue.get("assignee", {})
                if isinstance(assignee, dict):
                    assignee_name = assignee.get("name", "Unassigned")
                else:
                    assignee_name = "Unassigned"
                priority = issue.get("priority", 0)
                priority_label = {1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}.get(
                    priority, "No priority"
                )
                sections.append(f"  - [{priority_label}] {title} (Assignee: {assignee_name})")

        priority_issues = data.get("priority_issues", [])
        sections.append(f"\nHIGH PRIORITY / URGENT ({len(priority_issues)}):")
        for issue in priority_issues[:10]:
            if isinstance(issue, dict):
                title = issue.get("title", "Untitled")
                state = issue.get("state", {})
                state_name = state.get("name", "") if isinstance(state, dict) else ""
                sections.append(f"  - {title} [{state_name}]")

        in_progress = data.get("in_progress", [])
        sections.append(f"\nIN PROGRESS ({len(in_progress)}):")
        for issue in in_progress[:10]:
            if isinstance(issue, dict):
                title = issue.get("title", "Untitled")
                sections.append(f"  - {title}")

        recent = data.get("recent_movement", [])
        sections.append(f"\nRECENT MOVEMENT - LAST 24H ({len(recent)}):")
        for issue in recent[:10]:
            if isinstance(issue, dict):
                title = issue.get("title", "Untitled")
                updated = issue.get("updatedAt", "")
                sections.append(f"  - {title} (updated: {updated})")

        return "\n".join(sections)
