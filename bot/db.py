from __future__ import annotations

import aiosqlite
from typing import Any, List
from loguru import logger

DB_NAME = "events.db"


class Database:
    @staticmethod
    async def init_db():
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    user_id INTEGER,
                    event_id TEXT,
                    event_datetime TEXT,
                    text TEXT,
                    remind_time TEXT,
                    PRIMARY KEY (user_id, event_id),
                    UNIQUE(user_id, event_datetime)
                )
                """
            )
            await db.commit()

            # Миграция старой схемы (date -> event_datetime)
            async with db.execute("PRAGMA table_info(events)") as cursor:
                columns = [row[1] for row in await cursor.fetchall()]

            if "date" in columns:
                logger.warning("Обнаружена старая схема таблицы events. Выполняется миграция.")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events_new (
                        user_id INTEGER,
                        event_id TEXT,
                        event_datetime TEXT,
                        text TEXT,
                        remind_time TEXT,
                        PRIMARY KEY (user_id, event_id),
                        UNIQUE(user_id, event_datetime)
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT OR IGNORE INTO events_new (user_id, event_id, event_datetime, text, remind_time)
                    SELECT user_id,
                           event_id,
                           substr(date, 7, 4) || '-' || substr(date, 4, 2) || '-' || substr(date, 1, 2) || ' 00:00',
                           text,
                           remind_time
                    FROM events
                    """
                )
                await db.execute("DROP TABLE events")
                await db.execute("ALTER TABLE events_new RENAME TO events")
                await db.commit()
                logger.info("Миграция таблицы events завершена.")

    @staticmethod
    async def execute_query(query: str, params: tuple = (), fetch: bool = False) -> Any:
        async with aiosqlite.connect(DB_NAME) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                if fetch:
                    return await cursor.fetchall()
                await db.commit()
                return cursor.rowcount


class EventManager:
    @staticmethod
    async def get_user_events(user_id: int) -> List[aiosqlite.Row]:
        return await Database.execute_query(
            "SELECT * FROM events WHERE user_id = ? ORDER BY event_datetime",
            (user_id,),
            fetch=True,
        )

    @staticmethod
    async def get_single_event(user_id: int, event_id: str) -> aiosqlite.Row | None:
        rows = await Database.execute_query(
            "SELECT * FROM events WHERE user_id = ? AND event_id = ?",
            (user_id, event_id),
            fetch=True,
        )
        return rows[0] if rows else None

    @staticmethod
    async def add_event(user_id: int, event_id: str, event_datetime: str, text: str = "Мое событие"):
        await Database.execute_query(
            "INSERT INTO events (user_id, event_id, event_datetime, text, remind_time) VALUES (?, ?, ?, ?, NULL)",
            (user_id, event_id, event_datetime, text),
        )

    @staticmethod
    async def update_event_reminder(user_id: int, event_id: str, remind_time: str) -> bool:
        return (
            await Database.execute_query(
                "UPDATE events SET remind_time = ? WHERE user_id = ? AND event_id = ?",
                (remind_time, user_id, event_id),
            )
            > 0
        )

    @staticmethod
    async def clear_event_reminder(user_id: int, event_id: str) -> bool:
        return (
            await Database.execute_query(
                "UPDATE events SET remind_time = NULL WHERE user_id = ? AND event_id = ?",
                (user_id, event_id),
            )
            > 0
        )

    @staticmethod
    async def delete_event(user_id: int, event_id: str) -> bool:
        return (
            await Database.execute_query("DELETE FROM events WHERE user_id = ? AND event_id = ?", (user_id, event_id))
            > 0
        )

    @staticmethod
    async def clear_user_events(user_id: int) -> int:
        return await Database.execute_query("DELETE FROM events WHERE user_id = ?", (user_id,))

    @staticmethod
    async def event_exists(user_id: int, event_datetime: str) -> bool:
        return bool(
            await Database.execute_query(
                "SELECT 1 FROM events WHERE user_id = ? AND event_datetime = ? LIMIT 1",
                (user_id, event_datetime),
                fetch=True,
            )
        )

    @staticmethod
    async def get_events_for_reminder() -> List[aiosqlite.Row]:
        from datetime import datetime

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        return await Database.execute_query(
            "SELECT * FROM events WHERE remind_time IS NOT NULL AND remind_time <= ?",
            (now_str,),
            fetch=True,
        )
