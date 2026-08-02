from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

ACTIVITY_AUTO_RAISE = "auto_raise"
ACTIVITY_MESSAGE = "message"
ACTIVITY_KINDS = frozenset({ACTIVITY_AUTO_RAISE, ACTIVITY_MESSAGE})


@dataclass(frozen=True, slots=True)
class CategorySchedule:
    category_id: int
    name: str
    enabled: bool
    times: tuple[str, ...]
    last_slot: str | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    last_wait_seconds: int | None
    next_allowed_at: datetime | None
    retry_at: datetime | None
    retry_claimed_until: datetime | None
    retry_attempts: int


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


@dataclass(frozen=True, slots=True)
class ActivityPoint:
    kind: str
    occurred_at: datetime
    category_id: int | None


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
                    last_slot TEXT,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    last_wait_seconds INTEGER,
                    next_allowed_at TEXT,
                    retry_at TEXT,
                    retry_claimed_until TEXT,
                    retry_attempts INTEGER NOT NULL DEFAULT 0
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

                CREATE TABLE IF NOT EXISTS activity_points (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('auto_raise', 'message')),
                    occurred_at INTEGER NOT NULL,
                    category_id INTEGER,
                    source_key TEXT,
                    UNIQUE(kind, source_key)
                );

                CREATE INDEX IF NOT EXISTS activity_points_time_idx
                ON activity_points(occurred_at);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(category_schedules)"
                ).fetchall()
            }
            migrations = {
                "last_attempt_at": "TEXT",
                "last_success_at": "TEXT",
                "last_error": "TEXT",
                "last_wait_seconds": "INTEGER",
                "next_allowed_at": "TEXT",
                "retry_at": "TEXT",
                "retry_claimed_until": "TEXT",
                "retry_attempts": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, data_type in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE category_schedules ADD COLUMN {column} {data_type}"
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS category_schedules_retry_idx
                ON category_schedules(enabled, retry_at, retry_claimed_until)
                """
            )
            # Recover failed raises created by older versions which did not have a
            # durable retry queue. If FunPay's cooldown has already elapsed, they
            # become due immediately after the upgrade.
            connection.execute(
                """
                UPDATE category_schedules
                SET retry_at = COALESCE(next_allowed_at, ?)
                WHERE last_error IS NOT NULL AND retry_at IS NULL
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )
            # No in-process task survives a restart, so stale leases can be
            # released immediately while keeping the durable retry time.
            connection.execute(
                "UPDATE category_schedules SET retry_claimed_until = NULL"
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
        def parse_datetime(column: str) -> datetime | None:
            value = row[column]
            return datetime.fromisoformat(str(value)) if value else None

        return CategorySchedule(
            category_id=int(row["category_id"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            times=tuple(json.loads(row["times_json"])),
            last_slot=row["last_slot"],
            last_attempt_at=parse_datetime("last_attempt_at"),
            last_success_at=parse_datetime("last_success_at"),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            last_wait_seconds=(
                int(row["last_wait_seconds"])
                if row["last_wait_seconds"] is not None
                else None
            ),
            next_allowed_at=parse_datetime("next_allowed_at"),
            retry_at=parse_datetime("retry_at"),
            retry_claimed_until=parse_datetime("retry_claimed_until"),
            retry_attempts=int(row["retry_attempts"] or 0),
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

    def claim_raise_slot(
        self,
        category_id: int,
        current_slot: str,
        *,
        retry_claimed_until: datetime | None = None,
    ) -> bool:
        """Atomically reserve a category/time slot so it runs at most once."""
        if retry_claimed_until is not None:
            if retry_claimed_until.tzinfo is None:
                retry_claimed_until = retry_claimed_until.replace(tzinfo=timezone.utc)
            else:
                retry_claimed_until = retry_claimed_until.astimezone(timezone.utc)
        claimed_until = retry_claimed_until.isoformat() if retry_claimed_until else None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE category_schedules
                SET last_slot = ?,
                    retry_claimed_until = CASE
                        WHEN retry_at IS NOT NULL AND ? IS NOT NULL THEN ?
                        ELSE retry_claimed_until
                    END
                WHERE category_id = ? AND enabled = 1
                  AND (last_slot IS NULL OR last_slot <> ?)
                """,
                (
                    current_slot,
                    claimed_until,
                    claimed_until,
                    category_id,
                    current_slot,
                ),
            )
        return bool(cursor.rowcount)

    def claim_due_raise_retries(
        self,
        now: datetime,
        *,
        lease_seconds: int = 15 * 60,
    ) -> list[CategorySchedule]:
        """Lease due retries so a crash recovers them without duplicate tasks."""
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        now_iso = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        claimed: list[CategorySchedule] = []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM category_schedules
                WHERE enabled = 1 AND retry_at IS NOT NULL AND retry_at <= ?
                  AND (
                    retry_claimed_until IS NULL OR retry_claimed_until <= ?
                  )
                ORDER BY name COLLATE NOCASE
                """,
                (now_iso, now_iso),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE category_schedules
                    SET retry_claimed_until = ?
                    WHERE category_id = ? AND retry_at IS NOT NULL AND retry_at <= ?
                      AND (
                        retry_claimed_until IS NULL OR retry_claimed_until <= ?
                      )
                    """,
                    (lease_until, int(row["category_id"]), now_iso, now_iso),
                )
                if cursor.rowcount:
                    claimed.append(self._category_from_row(row))
        return claimed

    def record_raise_result(
        self,
        category_id: int,
        *,
        success: bool,
        error: str | None = None,
        wait_seconds: int | None = None,
        attempted_at: datetime | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        attempted_at = attempted_at or datetime.now(timezone.utc)
        if attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)
        else:
            attempted_at = attempted_at.astimezone(timezone.utc)
        if retry_at is not None and retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        elif retry_at is not None:
            retry_at = retry_at.astimezone(timezone.utc)
        normalized_wait = (
            max(0, int(wait_seconds)) if wait_seconds is not None else None
        )
        next_allowed_at = (
            attempted_at + timedelta(seconds=normalized_wait)
            if normalized_wait is not None
            else None
        )
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE category_schedules
                SET last_attempt_at = ?,
                    last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END,
                    last_error = ?,
                    last_wait_seconds = ?,
                    next_allowed_at = ?,
                    retry_at = CASE WHEN ? = 1 THEN NULL ELSE ? END,
                    retry_claimed_until = NULL,
                    retry_attempts = CASE
                        WHEN ? = 1 THEN 0
                        ELSE retry_attempts + 1
                    END
                WHERE category_id = ?
                """,
                (
                    attempted_at.isoformat(),
                    int(success),
                    attempted_at.isoformat(),
                    None if success else (error or "Неизвестная ошибка"),
                    normalized_wait,
                    next_allowed_at.isoformat() if next_allowed_at else None,
                    int(success),
                    retry_at.isoformat() if retry_at else None,
                    int(success),
                    category_id,
                ),
            )
        if not cursor.rowcount:
            raise KeyError(category_id)

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
                WHERE id = ? AND is_first = 1 AND acknowledged_at IS NULL
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
                WHERE id = ? AND is_first = 1
                  AND acknowledged_at IS NULL AND stopped_at IS NULL
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
                WHERE is_first = 1
                  AND acknowledged_at IS NULL AND stopped_at IS NULL
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

    def add_activity_point(
        self,
        kind: str,
        *,
        occurred_at: datetime | None = None,
        category_id: int | None = None,
        source_key: str | None = None,
    ) -> bool:
        if kind not in ACTIVITY_KINDS:
            raise ValueError(f"Неизвестный тип точки статистики: {kind}")
        occurred_at = occurred_at or datetime.now(timezone.utc)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        timestamp = int(occurred_at.timestamp())
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO activity_points(
                    kind, occurred_at, category_id, source_key
                ) VALUES(?, ?, ?, ?)
                """,
                (kind, timestamp, category_id, source_key),
            )
        return bool(cursor.rowcount)

    def list_activity_points(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ActivityPoint]:
        conditions: list[str] = []
        parameters: list[int] = []
        if since is not None:
            conditions.append("occurred_at >= ?")
            parameters.append(int(since.timestamp()))
        if until is not None:
            conditions.append("occurred_at < ?")
            parameters.append(int(until.timestamp()))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT kind, occurred_at, category_id FROM activity_points"
                + where
                + " ORDER BY occurred_at, id",
                parameters,
            ).fetchall()
        return [
            ActivityPoint(
                kind=str(row["kind"]),
                occurred_at=datetime.fromtimestamp(
                    int(row["occurred_at"]), tz=timezone.utc
                ),
                category_id=(
                    int(row["category_id"]) if row["category_id"] is not None else None
                ),
            )
            for row in rows
        ]
