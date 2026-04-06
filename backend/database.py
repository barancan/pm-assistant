import aiosqlite
import uuid
from datetime import datetime, timezone
from typing import Optional
import os

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./backend/pm_assistant.db")

_db_path: Optional[str] = None


def set_db_path(path: str) -> None:
    global _db_path
    _db_path = path


def get_db_path() -> str:
    return _db_path or DATABASE_PATH


async def init_db() -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        # One-time migration: old schema had auto-increment 'id' PK causing duplicates.
        # Detect it and collapse to one row per stage_number before recreating the table.
        async with db.execute("PRAGMA table_info(icm_stages)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "id" in cols and "stage_number" in cols:
            await db.execute("""
                CREATE TABLE icm_stages_new (
                    stage_number INTEGER PRIMARY KEY,
                    stage_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_run_at TEXT,
                    output_path TEXT
                )
            """)
            await db.execute("""
                INSERT OR IGNORE INTO icm_stages_new
                    (stage_number, stage_name, status, last_run_at, output_path)
                SELECT stage_number, stage_name, status, last_run_at, output_path
                FROM icm_stages
                GROUP BY stage_number
            """)
            await db.execute("DROP TABLE icm_stages")
            await db.execute("ALTER TABLE icm_stages_new RENAME TO icm_stages")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS processes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                started_at TEXT,
                completed_at TEXT,
                output_summary TEXT,
                error_message TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                content_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS icm_stages (
                stage_number INTEGER PRIMARY KEY,
                stage_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'idle',
                last_run_at TEXT,
                output_path TEXT
            )
        """)

        # Insert ICM stages if not present
        stages = [
            (1, "intake"),
            (2, "discovery"),
            (3, "opportunity"),
            (4, "prd"),
            (5, "critique"),
            (6, "stories"),
        ]
        for stage_number, stage_name in stages:
            await db.execute(
                """
                INSERT OR IGNORE INTO icm_stages (stage_number, stage_name, status)
                VALUES (?, ?, 'idle')
                """,
                (stage_number, stage_name),
            )

        await db.commit()


async def get_all_processes() -> list[dict]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM processes ORDER BY started_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def upsert_process(
    id: str,
    name: str,
    type: str,
    status: str,
    started_at: Optional[str] = None,
    completed_at: Optional[str] = None,
    output_summary: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            """
            INSERT INTO processes (id, name, type, status, started_at, completed_at, output_summary, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                type=excluded.type,
                status=excluded.status,
                started_at=COALESCE(excluded.started_at, started_at),
                completed_at=excluded.completed_at,
                output_summary=excluded.output_summary,
                error_message=excluded.error_message
            """,
            (id, name, type, status, started_at, completed_at, output_summary, error_message),
        )
        await db.commit()


async def get_latest_report(report_type: str) -> Optional[dict]:
    import json

    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reports WHERE type = ? ORDER BY created_at DESC LIMIT 1",
            (report_type,),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            result["content"] = json.loads(result["content_json"])
            return result


async def save_report(report_type: str, content_dict: dict) -> str:
    import json

    report_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "INSERT INTO reports (id, type, content_json, created_at) VALUES (?, ?, ?, ?)",
            (report_id, report_type, json.dumps(content_dict), created_at),
        )
        await db.commit()
    return report_id


async def get_chat_history(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chat_messages ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return list(reversed([dict(row) for row in rows]))


async def save_chat_message(role: str, content: str) -> str:
    message_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        await db.execute(
            "INSERT INTO chat_messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (message_id, role, content, created_at),
        )
        await db.commit()
    return message_id


async def get_all_icm_stages() -> list[dict]:
    async with aiosqlite.connect(get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM icm_stages ORDER BY stage_number ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def update_icm_stage(
    stage_number: int,
    status: str,
    output_path: Optional[str] = None,
) -> None:
    last_run_at = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(get_db_path()) as db:
        if output_path is not None:
            await db.execute(
                """
                UPDATE icm_stages
                SET status = ?, last_run_at = ?, output_path = ?
                WHERE stage_number = ?
                """,
                (status, last_run_at, output_path, stage_number),
            )
        else:
            await db.execute(
                """
                UPDATE icm_stages
                SET status = ?, last_run_at = ?
                WHERE stage_number = ?
                """,
                (status, last_run_at, stage_number),
            )
        await db.commit()
