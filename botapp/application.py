from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, InlineKeyboardMarkup

from .config import AppConfig
from .core import shorten, slot_key
from .database import (
    ACTIVITY_AUTO_RAISE,
    ACTIVITY_MESSAGE,
    CategorySchedule,
    Database,
)
from .funpay_bridge import FunPayBridge, FunPayNotConnected
from .reminders import ReminderManager, acknowledgement_keyboard

logger = logging.getLogger(__name__)


class AutomationApp:
    def __init__(self, config: AppConfig):
        self.config = config
        self.database = Database(config.database_path)
        self.database.initialize()
        self.bot = Bot(
            config.telegram_token,
            default=DefaultBotProperties(
                parse_mode=ParseMode.HTML,
                link_preview_is_disabled=True,
            ),
        )
        self.funpay = FunPayBridge(
            config.funpay_golden_key,
            config.funpay_user_agent,
            config.funpay_poll_seconds,
        )
        self.reminders = ReminderManager(self.bot, self.database, config.admin_id)
        self.dispatcher: Dispatcher | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._raise_lock = asyncio.Lock()
        self._last_raise_finished_monotonic: float | None = None
        self._last_connection_error: str | None = None

    async def start(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="menu", description="Открыть панель"),
                BotCommand(command="status", description="Статус бота"),
                BotCommand(command="stop", description="Остановить все напоминания"),
                BotCommand(command="cancel", description="Отменить ввод настройки"),
            ]
        )
        await self.reminders.restore()
        self._tasks = [
            asyncio.create_task(self._connection_loop(), name="funpay-connect"),
            asyncio.create_task(self._events_loop(), name="funpay-events"),
            asyncio.create_task(self._scheduler_loop(), name="lot-scheduler"),
            asyncio.create_task(self._session_refresh_loop(), name="session-refresh"),
        ]
        await self._safe_admin_message("🤖 Бот запущен. Подключаю FunPay…")

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.reminders.shutdown()
        await self.bot.session.close()

    async def _safe_admin_message(self, text: str, **kwargs: Any) -> Any | None:
        try:
            return await self.bot.send_message(self.config.admin_id, text, **kwargs)
        except Exception:
            logger.exception("Не удалось отправить служебное сообщение администратору")
            return None

    async def _connection_loop(self) -> None:
        while not self.funpay.connected:
            try:
                await self.funpay.connect()
                count = await self.sync_categories()
                self._last_connection_error = None
                await self._safe_admin_message(
                    "✅ FunPay подключён\n"
                    f"Аккаунт: <b>{html.escape(self.funpay.username or '—')}</b>\n"
                    f"Категорий с лотами: <b>{count}</b>"
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self.funpay.last_error = error
                logger.exception("Ошибка подключения FunPay")
                if error != self._last_connection_error:
                    self._last_connection_error = error
                    await self._safe_admin_message(
                        "⚠️ Не удалось подключить FunPay. Повторю через 60 секунд.\n"
                        f"<code>{html.escape(shorten(error, 500))}</code>"
                    )
                await asyncio.sleep(60)

    async def sync_categories(self) -> int:
        categories = await self.funpay.list_lot_categories()
        return self.database.sync_categories(categories)

    async def _events_loop(self) -> None:
        while True:
            message = await self.funpay.next_event()
            try:
                await self._process_funpay_message(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка обработки события FunPay")

    async def _process_funpay_message(self, message: Any) -> None:
        if message.author_id == 0 or message.author_id == self.funpay.account_id:
            return
        if getattr(message, "by_bot", False) or getattr(message, "by_vertex", False):
            return

        self.database.add_activity_point(
            ACTIVITY_MESSAGE,
            occurred_at=datetime.now(timezone.utc),
            source_key=f"{message.chat_id}:{message.id}",
        )

        author = message.author or message.chat_name or "Без имени"
        user_id = message.interlocutor_id or message.author_id or None
        is_first = not self.database.contact_exists(str(message.chat_id))
        if is_first:
            try:
                is_first = await self.funpay.is_first_user_message(
                    message.chat_id,
                    message.id,
                    message.chat_name or author,
                )
            except Exception:
                # A brand-new chat is still treated as first if history verification is temporarily unavailable.
                logger.warning(
                    "Не удалось перепроверить историю нового чата %s",
                    message.chat_id,
                    exc_info=True,
                )
        self.database.register_contact(str(message.chat_id), user_id, author)

        if is_first and self.database.get_bool("auto_reply_enabled", True):
            reply_text = self.database.get_setting("auto_reply_text", "") or ""
            if reply_text:
                try:
                    await self.funpay.send_message(
                        message.chat_id,
                        reply_text,
                        chat_name=message.chat_name,
                        interlocutor_id=message.interlocutor_id,
                    )
                except Exception as exc:
                    logger.exception("Не удалось отправить автоответ")
                    await self._safe_admin_message(
                        "⚠️ Автоответ не отправлен пользователю "
                        f"<b>{html.escape(author)}</b>: "
                        f"<code>{html.escape(shorten(str(exc), 400))}</code>"
                    )

        if not self.database.get_bool("notifications_enabled", True):
            return

        notification = self.database.create_notification(
            chat_id=str(message.chat_id),
            user_id=user_id,
            author=author,
            message_text=message.text,
            funpay_message_id=message.id,
            is_first=is_first,
        )
        keyboard = acknowledgement_keyboard(notification.id) if is_first else None
        telegram_message_id = await self._send_funpay_notification(
            message, author, user_id, keyboard
        )
        if telegram_message_id is not None:
            self.database.set_telegram_message_id(notification.id, telegram_message_id)

        if is_first and self.database.get_bool("reminders_enabled", True):
            self.reminders.start(notification.id)

    async def _send_funpay_notification(
        self,
        message: Any,
        author: str,
        user_id: int | None,
        keyboard: InlineKeyboardMarkup | None,
    ) -> int | None:
        profile_url = (
            f"https://funpay.com/users/{user_id}/" if user_id else "неизвестен"
        )
        header = f"🔔 Новое сообщение FunPay\nАвтор: {author}\nПрофиль: {profile_url}"
        content = message.text or "[изображение без текста]"
        image_link = getattr(message, "image_link", None)

        if image_link:
            caption = f"{header}\n\n{content}"
            if len(caption) <= 1024:
                try:
                    sent = await self.bot.send_photo(
                        self.config.admin_id,
                        image_link,
                        caption=caption,
                        parse_mode=None,
                        reply_markup=keyboard,
                    )
                    return sent.message_id
                except TelegramBadRequest:
                    logger.warning(
                        "Telegram не смог скачать изображение FunPay", exc_info=True
                    )
            else:
                try:
                    await self.bot.send_photo(
                        self.config.admin_id,
                        image_link,
                        caption=header,
                        parse_mode=None,
                    )
                except TelegramBadRequest:
                    logger.warning(
                        "Telegram не смог скачать изображение FunPay", exc_info=True
                    )

        return await self._send_text_chunks(header, content, keyboard)

    async def _send_text_chunks(
        self,
        header: str,
        content: str,
        keyboard: InlineKeyboardMarkup | None,
    ) -> int | None:
        prefix = header + "\n\n"
        first_room = 4096 - len(prefix)
        chunks: list[str] = []
        if first_room > 0:
            chunks.append(prefix + content[:first_room])
            content = content[first_room:]
        else:
            chunks.append(header[:4096])
        while content:
            chunks.append(content[:4096])
            content = content[4096:]

        last_id: int | None = None
        for index, chunk in enumerate(chunks):
            sent = await self.bot.send_message(
                self.config.admin_id,
                chunk,
                parse_mode=None,
                reply_markup=keyboard if index == len(chunks) - 1 else None,
            )
            last_id = sent.message_id
        return last_id

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                now = datetime.now(self.config.timezone)
                current_time = now.strftime("%H:%M")
                current_slot = slot_key(now)
                if self.funpay.connected:
                    claimed: list[CategorySchedule] = []
                    for category in self.database.list_categories():
                        if (
                            category.enabled
                            and current_time in category.times
                            and self.database.claim_raise_slot(
                                category.category_id, current_slot
                            )
                        ):
                            claimed.append(category)
                    if claimed:
                        asyncio.create_task(
                            self._raise_batch(claimed),
                            name=f"raise-batch-{current_slot}",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка планировщика поднятий")
            await asyncio.sleep(10)

    async def _raise_batch(self, categories: list[CategorySchedule]) -> None:
        for category in categories:
            await self._raise_and_record(category, scheduled=True)

    async def _call_raise_with_spacing(
        self, category_id: int, *, minimum_wait: float = 0.0
    ) -> int | None:
        if self._last_raise_finished_monotonic is not None:
            elapsed = time.monotonic() - self._last_raise_finished_monotonic
            required = max(
                self.config.category_raise_delay_seconds,
                max(0.0, minimum_wait),
            )
            if elapsed < required:
                await asyncio.sleep(required - elapsed)
        try:
            return await self.funpay.raise_category(category_id)
        finally:
            # The delay is measured after the previous request has completed. This
            # makes it accumulate correctly for batches of any size.
            self._last_raise_finished_monotonic = time.monotonic()

    @staticmethod
    def _raise_error(exc: Exception) -> tuple[str, int | None]:
        raw_wait = getattr(exc, "wait_time", None)
        try:
            wait = max(0, int(raw_wait)) if raw_wait is not None else None
        except (TypeError, ValueError):
            wait = None
        detail = getattr(exc, "error_message", None) or str(exc) or type(exc).__name__
        return detail, wait

    def _record_raise_success(
        self,
        category: CategorySchedule,
        wait_seconds: int | None,
        *,
        scheduled: bool,
    ) -> None:
        occurred_at = datetime.now(timezone.utc)
        self.database.record_raise_result(
            category.category_id,
            success=True,
            wait_seconds=wait_seconds,
            attempted_at=occurred_at,
        )
        if scheduled:
            self.database.add_activity_point(
                ACTIVITY_AUTO_RAISE,
                occurred_at=occurred_at,
                category_id=category.category_id,
            )

    async def _raise_and_record(
        self, category: CategorySchedule, *, scheduled: bool
    ) -> bool:
        async with self._raise_lock:
            source = "по расписанию" if scheduled else "вручную"
            try:
                wait_seconds = await self._call_raise_with_spacing(category.category_id)
            except Exception as exc:  # noqa: BLE001 - persist any upstream raise failure.
                detail, wait = self._raise_error(exc)
                if wait is not None and 0 < wait <= 10:
                    logger.warning(
                        "FunPay попросил подождать %s сек. для категории %s; повторяю один раз",
                        wait,
                        category.category_id,
                    )
                    try:
                        wait_seconds = await self._call_raise_with_spacing(
                            category.category_id,
                            minimum_wait=wait,
                        )
                    except Exception as retry_exc:  # noqa: BLE001 - persist retry failure too.
                        detail, wait = self._raise_error(retry_exc)
                    else:
                        self._record_raise_success(
                            category,
                            wait_seconds,
                            scheduled=scheduled,
                        )
                        logger.info(
                            "Категория %s поднята %s после автоматического повтора",
                            category.category_id,
                            source,
                        )
                        return True
                self.database.record_raise_result(
                    category.category_id,
                    success=False,
                    error=detail,
                    wait_seconds=wait,
                    attempted_at=datetime.now(timezone.utc),
                )
                logger.error(
                    "Категория %s не поднята %s: %s",
                    category.category_id,
                    source,
                    detail,
                )
                return False

            self._record_raise_success(
                category,
                wait_seconds,
                scheduled=scheduled,
            )
            logger.info(
                "Категория %s поднята %s",
                category.category_id,
                source,
            )
            return True

    async def raise_now(self, category_id: int) -> bool:
        category = self.database.get_category(category_id)
        if category is None:
            raise KeyError(category_id)
        if not self.funpay.connected:
            raise FunPayNotConnected("FunPay ещё не подключён.")
        return await self._raise_and_record(category, scheduled=False)

    async def _session_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(45 * 60)
            if not self.funpay.connected:
                continue
            try:
                await self.funpay.refresh_session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.funpay.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Не удалось обновить сессию FunPay")
                await self._safe_admin_message(
                    "⚠️ Не удалось обновить сессию FunPay: "
                    f"<code>{html.escape(shorten(str(exc), 500))}</code>"
                )

    def status_text(self) -> str:
        categories = self.database.list_categories()
        enabled = sum(1 for category in categories if category.enabled)
        last_event = "ещё не было"
        if self.funpay.last_event_at:
            local = self.funpay.last_event_at.astimezone(self.config.timezone)
            last_event = local.strftime("%d.%m.%Y %H:%M:%S")
        state = "🟢 подключён" if self.funpay.connected else "🔴 не подключён"
        auto_reply = (
            "включён"
            if self.database.get_bool("auto_reply_enabled", True)
            else "выключен"
        )
        notifications = (
            "включены"
            if self.database.get_bool("notifications_enabled", True)
            else "выключены"
        )
        reminders = (
            "включены"
            if self.database.get_bool("reminders_enabled", True)
            else "выключены"
        )
        return (
            "<b>Статус</b>\n\n"
            f"FunPay: {state}\n"
            f"Аккаунт: <b>{html.escape(self.funpay.username or '—')}</b>\n"
            f"Последнее событие: {last_event}\n"
            f"Часовой пояс: <code>{self.config.timezone_name}</code>\n\n"
            f"Категории: {enabled}/{len(categories)} включено\n"
            f"Автоответ: {auto_reply}\n"
            f"Уведомления: {notifications}\n"
            f"Напоминания: {reminders}\n"
            f"Ждут подтверждения: {self.database.count_pending_notifications()}"
        )
