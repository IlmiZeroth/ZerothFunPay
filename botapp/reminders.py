from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .core import shorten
from .database import Database


def acknowledgement_keyboard(
    notification_id: str, *, include_stop: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="✅ Подтвердить получение",
                callback_data=f"ack:{notification_id}",
            )
        ]
    ]
    if include_stop:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🛑 Остановить эти напоминания",
                    callback_data=f"remstop:{notification_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


class ReminderManager:
    def __init__(self, bot: Bot, database: Database, admin_id: int):
        self.bot = bot
        self.database = database
        self.admin_id = admin_id
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def restore(self) -> None:
        if not self.database.get_bool("reminders_enabled", True):
            return
        for notification in self.database.list_pending_notifications():
            self.start(notification.id)

    def start(self, notification_id: str) -> None:
        if notification_id in self.tasks and not self.tasks[notification_id].done():
            return
        task = asyncio.create_task(
            self._run(notification_id), name=f"reminder-{notification_id}"
        )
        self.tasks[notification_id] = task

    async def _run(self, notification_id: str) -> None:
        try:
            notification = self.database.get_pending_notification(notification_id)
            if notification is None:
                return

            delay_minutes = max(
                0.1, self.database.get_float("reminder_delay_minutes", 7.0)
            )
            due_at = notification.created_at.timestamp() + delay_minutes * 60
            wait_seconds = max(0.0, due_at - datetime.now(timezone.utc).timestamp())
            await asyncio.sleep(wait_seconds)

            while self.database.get_bool("reminders_enabled", True):
                notification = self.database.get_pending_notification(notification_id)
                if notification is None:
                    return

                preview = shorten(notification.message_text or "[изображение]", 500)
                text = (
                    "🚨 НЕ ПОДТВЕРЖДЕНО СООБЩЕНИЕ FUNPAY\n"
                    f"Автор: {notification.author}\n\n{preview}"
                )
                try:
                    await self.bot.send_message(
                        self.admin_id,
                        text,
                        parse_mode=None,
                        reply_markup=acknowledgement_keyboard(
                            notification_id, include_stop=True
                        ),
                    )
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(float(exc.retry_after) + 0.2)
                    continue
                except TelegramForbiddenError:
                    return

                # Official guidance for a single private chat is no more than about 1 msg/sec.
                interval = max(
                    1.05, self.database.get_float("reminder_interval_seconds", 1.1)
                )
                await asyncio.sleep(interval)
        finally:
            current = self.tasks.get(notification_id)
            if current is asyncio.current_task():
                self.tasks.pop(notification_id, None)

    def confirm(self, notification_id: str) -> bool:
        changed = self.database.acknowledge_notification(notification_id)
        self._cancel(notification_id)
        return changed

    def stop_one(self, notification_id: str) -> bool:
        changed = self.database.stop_notification(notification_id)
        self._cancel(notification_id)
        return changed

    def stop_all(self) -> int:
        stopped = self.database.stop_all_notifications()
        for notification_id in list(self.tasks):
            self._cancel(notification_id)
        return stopped

    def _cancel(self, notification_id: str) -> None:
        task = self.tasks.pop(notification_id, None)
        if task and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
