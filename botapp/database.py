from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .core import DEFAULT_RAISE_TIMES, parse_daily_times

DEFAULT_SETTINGS = {
    "auto_reply_enabled": "1",
    "auto_reply_text": (
        "Здравствуйте, получил ваше сообщение. Уточните задачу, необходимые функции и бюджет. "
        "На ответ обычно требуется от 10 минут до 2 часов."
    ),
    "notifications_enabled": "1",
    "reminders_enabled": "1",
    "reminder_delay_minutes": "7",
    "reminder_interval_seconds": "1.1",
}


@dataclass(frozen=True, slots=True)
class CategorySchedule:
    category_id: int
    name: str
    enabled: bool
    times: tuple[str, ...]
    last_slot: str | None


@dataclass(frozen=True, slots=True)
class PendingNotification:
    id: str
    chat_id: str
    user_id: int | None
    author: str
    message_text: str | None
    funpay_message_id: int | None
    telegram_message_id: int | None
    created_at: datetime


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS category_schedules (
                    category_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    times_json TEXT NOT NULL,
                    last_slot TEXT
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    chat_id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    username TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    user_id INTEGER,
                    author TEXT NOT NULL,
                    message_text TEXT,
                    funpay_message_id INTEGER,
                    is_first INTEGER NOT NULL,
                    telegram_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    stopped_at TEXT
                );

                CREATE INDEX IF NOT EXISTS notifications_pending_idx
                ON notifications(is_first, acknowledged_at, stopped_at);
                """
            )
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                DEFAULT_SETTINGS.items(),
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get_setting(key)
        if value is None:
            return default
        return value.lower() in {"1", "true", "yes", "on"}

    def set_bool(self, key: str, value: bool) -> None:
        self.set_setting(key, "1" if value else "0")

    def get_float(self, key: str, default: float) -> float:
        value = self.get_setting(key)
        try:
            return float(value) if value is not None else default
        except ValueError:
            return default

    def sync_categories(self, categories: list[tuple[int, str]]) -> int:
        payload = json.dumps(DEFAULT_RAISE_TIMES, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            for category_id, name in categories:
                connection.execute(
                    """
                    INSERT INTO category_schedules(category_id, name, enabled, times_json)
                    VALUES(?, ?, 1, ?)
                    ON CONFLICT(category_id) DO UPDATE SET name = excluded.name
                    """,
                    (category_id, name, payload),
                )
        return len(categories)

    @staticmethod
    def _category_from_row(row: sqlite3.Row) -> CategorySchedule:
        return CategorySchedule(
            category_id=int(row["category_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            times=tuple(json.loads(row["times_json"])),
            last_slot=row["last_slot"],
        )

    def list_categories(self) -> list[CategorySchedule]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM category_schedules ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._category_from_row(row) for row in rows]

    def get_category(self, category_id: int) -> CategorySchedule | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM category_schedules WHERE category_id = ?", (category_id,)
            ).fetchone()
        return self._category_from_row(row) if row else None

    def toggle_category(self, category_id: int) -> bool:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE category_schedules SET enabled = NOT enabled WHERE category_id = ?",
                (category_id,),
            )
            row = connection.execute(
                "SELECT enabled FROM category_schedules WHERE category_id = ?",
                (category_id,),
            ).fetchone()
        if not row:
            raise KeyError(category_id)
        return bool(row["enabled"])

    def update_category_times(self, category_id: int, times: list[str]) -> None:
        normalized = parse_daily_times(times)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE category_schedules SET times_json = ? WHERE category_id = ?",
                (json.dumps(normalized, ensure_ascii=False), category_id),
            )
            if not cursor.rowcount:
                raise KeyError(category_id)

    def claim_raise_slot(self, category_id: int, current_slot: str) -> bool:
        """Atomically reserve a category/time slot so it runs at most once."""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE category_schedules
                SET last_slot = ?
                WHERE category_id = ? AND enabled = 1
                  AND (last_slot IS NULL OR last_slot <> ?)
                """,
                (current_slot, category_id, current_slot),
            )
        return bool(cursor.rowcount)

    def register_contact(
        self, chat_id: str, user_id: int | None, username: str
    ) -> bool:
        """Register a chat and return True only on the first registration."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO contacts(chat_id, user_id, username, first_seen_at) VALUES(?, ?, ?, ?)",
                (str(chat_id), user_id, username, now),
            )
            is_first = bool(cursor.rowcount)
            if not is_first:
                connection.execute(
                    "UPDATE contacts SET user_id = COALESCE(?, user_id), username = ? WHERE chat_id = ?",
                    (user_id, username, str(chat_id)),
                )
        return is_first

    def contact_exists(self, chat_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM contacts WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
        return row is not None

    def create_notification(
        self,
        *,
        chat_id: str,
        user_id: int | None,
        author: str,
        message_text: str | None,
        funpay_message_id: int | None,
        is_first: bool,
    ) -> PendingNotification:
        notification_id = uuid.uuid4().hex[:16]
        created_at = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO notifications(
                    id, chat_id, user_id, author, message_text, funpay_message_id,
                    is_first, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    str(chat_id),
                    user_id,
                    author,
                    message_text,
                    funpay_message_id,
                    int(is_first),
                    created_at.isoformat(),
                ),
            )
        return PendingNotification(
            id=notification_id,
            chat_id=str(chat_id),
            user_id=user_id,
            author=author,
            message_text=message_text,
            funpay_message_id=funpay_message_id,
            telegram_message_id=None,
            created_at=created_at,
        )

    def set_telegram_message_id(self, notification_id: str, message_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE notifications SET telegram_message_id = ? WHERE id = ?",
                (message_id, notification_id),
            )

    def get_pending_notification(
        self, notification_id: str
    ) -> PendingNotification | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM notifications
                WHERE id = ? AND is_first = 1
                  AND acknowledged_at IS NULL AND stopped_at IS NULL
                """,
                (notification_id,),
            ).fetchone()
        return self._notification_from_row(row) if row else None

    def list_pending_notifications(self) -> list[PendingNotification]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM notifications
                WHERE is_first = 1 AND acknowledged_at IS NULL AND stopped_at IS NULL
                ORDER BY created_at
                """
            ).fetchall()
        return [self._notification_from_row(row) for row in rows]

    @staticmethod
    def _notification_from_row(row: sqlite3.Row) -> PendingNotification:
        return PendingNotification(
            id=str(row["id"]),
            chat_id=str(row["chat_id"]),
            user_id=int(row["user_id"]) if row["user_id"] is not None else None,
            author=str(row["author"]),
            message_text=row["message_text"],
            funpay_message_id=(
                int(row["funpay_message_id"])
                if row["funpay_message_id"] is not None
                else None
            ),
            telegram_message_id=(
                int(row["telegram_message_id"])
                if row["telegram_message_id"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def acknowledge_notification(self, notification_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notifications SET acknowledged_at = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (now, notification_id),
            )
        return bool(cursor.rowcount)

    def stop_notification(self, notification_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notifications SET stopped_at = ?
                WHERE id = ? AND acknowledged_at IS NULL AND stopped_at IS NULL
                """,
                (now, notification_id),
            )
        return bool(cursor.rowcount)

    def stop_all_notifications(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notifications SET stopped_at = ?
                WHERE acknowledged_at IS NULL AND stopped_at IS NULL
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def count_pending_notifications(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total FROM notifications
                WHERE is_first = 1 AND acknowledged_at IS NULL AND stopped_at IS NULL
                """
            ).fetchone()
        return int(row["total"])
