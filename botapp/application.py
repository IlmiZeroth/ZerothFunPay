from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BotCommand, InlineKeyboardMarkup

from .config import AppConfig
from .core import shorten, slot_key
from .database import CategorySchedule, Database
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
                    for category in self.database.list_categories():
                        if (
                            category.enabled
                            and current_time in category.times
                            and self.database.claim_raise_slot(
                                category.category_id, current_slot
                            )
                        ):
                            asyncio.create_task(
                                self._raise_and_report(category, scheduled=True),
                                name=f"raise-{category.category_id}-{current_slot}",
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка планировщика поднятий")
            await asyncio.sleep(10)

    async def _raise_and_report(
        self, category: CategorySchedule, *, scheduled: bool
    ) -> str:
        async with self._raise_lock:
            source = "по расписанию" if scheduled else "вручную"
            try:
                wait_seconds = await self.funpay.raise_category(category.category_id)
                wait_text = (
                    f" Следующее разрешённое поднятие примерно через {wait_seconds} сек."
                    if wait_seconds
                    else ""
                )
                text = f"⬆️ <b>{html.escape(category.name)}</b>: лоты подняты {source}.{wait_text}"
            except Exception as exc:
                logger.exception("Ошибка поднятия категории %s", category.category_id)
                wait = getattr(exc, "wait_time", None)
                detail = (
                    getattr(exc, "error_message", None)
                    or str(exc)
                    or type(exc).__name__
                )
                suffix = f" Ожидание FunPay: {wait} сек." if wait else ""
                text = (
                    f"⚠️ <b>{html.escape(category.name)}</b>: поднятие {source} не выполнено. "
                    f"<code>{html.escape(shorten(detail, 600))}</code>{suffix}"
                )
            await self._safe_admin_message(text)
            return text

    async def raise_now(self, category_id: int) -> str:
        category = self.database.get_category(category_id)
        if category is None:
            raise KeyError(category_id)
        if not self.funpay.connected:
            raise FunPayNotConnected("FunPay ещё не подключён.")
        return await self._raise_and_report(category, scheduled=False)

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
