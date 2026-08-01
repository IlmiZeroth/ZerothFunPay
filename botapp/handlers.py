from __future__ import annotations

import html
import math

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .core import parse_daily_times, shorten
from .funpay_bridge import FunPayNotConnected


class InputState(StatesGroup):
    auto_reply_text = State()
    reminder_delay = State()
    reminder_interval = State()
    category_times = State()


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("📊 Статус", "status"), _button("🗂 Категории", "cats:0")],
            [_button("💬 Автоответ", "auto"), _button("🔔 Уведомления", "notify")],
            [_button("🚨 Напоминания", "reminders")],
            [_button("🛑 Остановить все напоминания", "remstopall")],
        ]
    )


def back_menu() -> list[InlineKeyboardButton]:
    return [_button("⬅️ В меню", "menu")]


def auto_reply_page(app: AutomationApp) -> tuple[str, InlineKeyboardMarkup]:
    enabled = app.database.get_bool("auto_reply_enabled", True)
    text = app.database.get_setting("auto_reply_text", "") or ""
    body = (
        "<b>Автоответ на первое сообщение</b>\n\n"
        f"Состояние: {'🟢 включён' if enabled else '🔴 выключен'}\n\n"
        f"Текст:\n<blockquote>{html.escape(shorten(text, 2500))}</blockquote>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Выключить" if enabled else "Включить", "auto:toggle")],
            [_button("✏️ Изменить текст", "auto:edit")],
            back_menu(),
        ]
    )
    return body, keyboard


def notification_page(app: AutomationApp) -> tuple[str, InlineKeyboardMarkup]:
    enabled = app.database.get_bool("notifications_enabled", True)
    body = (
        "<b>Уведомления о сообщениях FunPay</b>\n\n"
        f"Состояние: {'🟢 включены' if enabled else '🔴 выключены'}\n\n"
        "FunPay не сообщает, открыт ли сайт именно в вашем браузере. Поэтому при включённой "
        "опции уведомления приходят всегда."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Выключить" if enabled else "Включить", "notify:toggle")],
            back_menu(),
        ]
    )
    return body, keyboard


def reminders_page(app: AutomationApp) -> tuple[str, InlineKeyboardMarkup]:
    enabled = app.database.get_bool("reminders_enabled", True)
    delay = app.database.get_float("reminder_delay_minutes", 7.0)
    interval = max(1.05, app.database.get_float("reminder_interval_seconds", 1.1))
    body = (
        "<b>Частые напоминания</b>\n\n"
        f"Состояние: {'🟢 включены' if enabled else '🔴 выключены'}\n"
        f"Старт через: <b>{delay:g} мин.</b>\n"
        f"Интервал: <b>{interval:g} сек.</b>\n"
        f"Ждут подтверждения: <b>{app.database.count_pending_notifications()}</b>\n\n"
        "Минимальный интервал — 1,05 секунды. При ответе Telegram 429 бот автоматически "
        "выдержит указанную сервером паузу."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Выключить" if enabled else "Включить", "reminders:toggle")],
            [_button("⏱ Задержка старта", "reminders:delay")],
            [_button("⚡ Интервал", "reminders:interval")],
            [_button("🛑 Остановить активные", "remstopall")],
            back_menu(),
        ]
    )
    return body, keyboard


def category_page(
    app: AutomationApp, category_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    category = app.database.get_category(category_id)
    if category is None:
        raise KeyError(category_id)
    body = (
        f"<b>{html.escape(category.name)}</b>\n\n"
        f"Подъём: {'🟢 включён' if category.enabled else '🔴 выключен'}\n"
        f"Расписание: <code>{' · '.join(category.times)}</code>\n"
        f"ID категории: <code>{category.category_id}</code>"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(
                    "Выключить" if category.enabled else "Включить",
                    f"cat:toggle:{category_id}",
                )
            ],
            [_button("🕒 Изменить время", f"cat:times:{category_id}")],
            [_button("⬆️ Поднять сейчас", f"cat:now:{category_id}")],
            [_button("⬅️ К категориям", "cats:0")],
        ]
    )
    return body, keyboard


def categories_page(app: AutomationApp, page: int) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 8
    categories = app.database.list_categories()
    total_pages = max(1, math.ceil(len(categories) / page_size))
    page = min(max(page, 0), total_pages - 1)
    start = page * page_size
    rows: list[list[InlineKeyboardButton]] = []
    for category in categories[start : start + page_size]:
        icon = "🟢" if category.enabled else "🔴"
        rows.append(
            [
                _button(
                    f"{icon} {shorten(category.name, 42)}",
                    f"cat:{category.category_id}",
                )
            ]
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(_button("◀️", f"cats:{page - 1}"))
        nav.append(_button(f"{page + 1}/{total_pages}", "noop"))
        if page + 1 < total_pages:
            nav.append(_button("▶️", f"cats:{page + 1}"))
        rows.append(nav)
    rows.append([_button("🔄 Синхронизировать с FunPay", "cats:sync")])
    rows.append(back_menu())
    body = (
        "<b>Категории и расписание подъёма</b>\n\n"
        "У каждой категории своё расписание. Новые категории получают стандарт: "
        "<code>06:37 · 10:37 · 14:37 · 18:37 · 22:37 · 02:37</code>."
    )
    if not categories:
        body += "\n\nКатегории ещё не загружены. Нажмите синхронизацию после подключения FunPay."
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def build_dispatcher(app: AutomationApp) -> Dispatcher:
    router = Router(name="admin-panel")
    router.message.filter(F.from_user.id == app.config.admin_id)
    router.callback_query.filter(F.from_user.id == app.config.admin_id)

    @router.message(CommandStart())
    @router.message(Command("menu"))
    async def menu_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "<b>Панель FunPay-бота</b>\n\nВыберите раздел:",
            reply_markup=main_menu(),
        )

    @router.message(Command("status"))
    async def status_message(message: Message) -> None:
        await message.answer(app.status_text(), reply_markup=main_menu())

    @router.message(Command("stop"))
    async def stop_message(message: Message) -> None:
        stopped = app.reminders.stop_all()
        await message.answer(
            f"🛑 Напоминания остановлены: {stopped}.", reply_markup=main_menu()
        )

    @router.message(Command("cancel"))
    async def cancel_input(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Ввод отменён.", reply_markup=main_menu())

    @router.message(InputState.auto_reply_text)
    async def receive_auto_reply(message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text:
            await message.answer(
                "Текст не может быть пустым. Отправьте текст или /cancel."
            )
            return
        if len(text) > 2000:
            await message.answer("Слишком длинный текст. Максимум — 2000 символов.")
            return
        app.database.set_setting("auto_reply_text", text)
        await state.clear()
        body, keyboard = auto_reply_page(app)
        await message.answer("✅ Текст сохранён.\n\n" + body, reply_markup=keyboard)

    @router.message(InputState.reminder_delay)
    async def receive_delay(message: Message, state: FSMContext) -> None:
        try:
            value = float((message.text or "").strip().replace(",", "."))
            if not 0.1 <= value <= 1440:
                raise ValueError
        except ValueError:
            await message.answer("Введите число от 0,1 до 1440 минут или /cancel.")
            return
        app.database.set_setting("reminder_delay_minutes", f"{value:g}")
        await state.clear()
        body, keyboard = reminders_page(app)
        await message.answer("✅ Задержка сохранена.\n\n" + body, reply_markup=keyboard)

    @router.message(InputState.reminder_interval)
    async def receive_interval(message: Message, state: FSMContext) -> None:
        try:
            value = float((message.text or "").strip().replace(",", "."))
            if not 1.05 <= value <= 60:
                raise ValueError
        except ValueError:
            await message.answer("Введите число от 1,05 до 60 секунд или /cancel.")
            return
        app.database.set_setting("reminder_interval_seconds", f"{value:g}")
        await state.clear()
        body, keyboard = reminders_page(app)
        await message.answer("✅ Интервал сохранён.\n\n" + body, reply_markup=keyboard)

    @router.message(InputState.category_times)
    async def receive_category_times(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        category_id = int(data["category_id"])
        try:
            times = parse_daily_times(message.text or "")
        except ValueError as exc:
            await message.answer(
                f"{html.escape(str(exc))}\nПример: <code>06:37, 10:37, 14:37</code>"
            )
            return
        app.database.update_category_times(category_id, times)
        await state.clear()
        body, keyboard = category_page(app, category_id)
        await message.answer(
            "✅ Расписание сохранено.\n\n" + body, reply_markup=keyboard
        )

    @router.callback_query(F.data == "menu")
    async def menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await _edit(
            callback, "<b>Панель FunPay-бота</b>\n\nВыберите раздел:", main_menu()
        )

    @router.callback_query(F.data == "status")
    async def status_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, app.status_text(), main_menu())

    @router.callback_query(F.data == "auto")
    async def auto_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, *auto_reply_page(app))

    @router.callback_query(F.data == "auto:toggle")
    async def auto_toggle(callback: CallbackQuery) -> None:
        enabled = not app.database.get_bool("auto_reply_enabled", True)
        app.database.set_bool("auto_reply_enabled", enabled)
        await callback.answer("Включено" if enabled else "Выключено")
        await _edit(callback, *auto_reply_page(app))

    @router.callback_query(F.data == "auto:edit")
    async def auto_edit(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(InputState.auto_reply_text)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отправьте новый текст автоответа. Для отмены: /cancel"
            )

    @router.callback_query(F.data == "notify")
    async def notify_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, *notification_page(app))

    @router.callback_query(F.data == "notify:toggle")
    async def notify_toggle(callback: CallbackQuery) -> None:
        enabled = not app.database.get_bool("notifications_enabled", True)
        app.database.set_bool("notifications_enabled", enabled)
        await callback.answer("Включены" if enabled else "Выключены")
        await _edit(callback, *notification_page(app))

    @router.callback_query(F.data == "reminders")
    async def reminders_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, *reminders_page(app))

    @router.callback_query(F.data == "reminders:toggle")
    async def reminders_toggle(callback: CallbackQuery) -> None:
        enabled = not app.database.get_bool("reminders_enabled", True)
        app.database.set_bool("reminders_enabled", enabled)
        if not enabled:
            app.reminders.stop_all()
        await callback.answer("Включены" if enabled else "Выключены")
        await _edit(callback, *reminders_page(app))

    @router.callback_query(F.data == "reminders:delay")
    async def reminders_delay(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(InputState.reminder_delay)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Через сколько минут запускать напоминания? Например: 7"
            )

    @router.callback_query(F.data == "reminders:interval")
    async def reminders_interval(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(InputState.reminder_interval)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Интервал в секундах (минимум 1,05). Например: 1,1"
            )

    @router.callback_query(F.data == "remstopall")
    async def reminders_stop_all(callback: CallbackQuery) -> None:
        stopped = app.reminders.stop_all()
        await callback.answer(f"Остановлено: {stopped}", show_alert=True)
        if callback.message:
            await callback.message.answer(
                f"🛑 Все активные напоминания остановлены: {stopped}."
            )

    @router.callback_query(F.data.startswith("ack:"))
    async def acknowledge(callback: CallbackQuery) -> None:
        notification_id = (callback.data or "").split(":", 1)[1]
        changed = app.reminders.confirm(notification_id)
        await callback.answer(
            "Получение подтверждено" if changed else "Уже подтверждено/остановлено"
        )
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    @router.callback_query(F.data.startswith("remstop:"))
    async def reminder_stop_one(callback: CallbackQuery) -> None:
        notification_id = (callback.data or "").split(":", 1)[1]
        changed = app.reminders.stop_one(notification_id)
        await callback.answer(
            "Напоминания остановлены" if changed else "Уже остановлено"
        )
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    @router.callback_query(F.data == "cats:sync")
    async def categories_sync(callback: CallbackQuery) -> None:
        await callback.answer("Синхронизирую…")
        try:
            count = await app.sync_categories()
            if callback.message:
                await callback.message.answer(
                    f"✅ Синхронизировано категорий: {count}."
                )
            await _edit(callback, *categories_page(app, 0))
        except FunPayNotConnected:
            if callback.message:
                await callback.message.answer(
                    "FunPay ещё не подключён. Попробуйте чуть позже."
                )
        except Exception as exc:  # noqa: BLE001 - surface any upstream/API parse failure in the admin UI.
            if callback.message:
                await callback.message.answer(
                    f"Ошибка синхронизации: <code>{html.escape(shorten(str(exc), 500))}</code>"
                )

    @router.callback_query(F.data.regexp(r"^cats:\d+$"))
    async def categories_callback(callback: CallbackQuery) -> None:
        page = int((callback.data or "0").split(":")[1])
        await callback.answer()
        await _edit(callback, *categories_page(app, page))

    @router.callback_query(F.data.regexp(r"^cat:\d+$"))
    async def category_callback(callback: CallbackQuery) -> None:
        category_id = int((callback.data or "0").split(":")[1])
        await callback.answer()
        try:
            await _edit(callback, *category_page(app, category_id))
        except KeyError:
            await callback.answer("Категория не найдена", show_alert=True)

    @router.callback_query(F.data.regexp(r"^cat:toggle:\d+$"))
    async def category_toggle(callback: CallbackQuery) -> None:
        category_id = int((callback.data or "0").rsplit(":", 1)[1])
        enabled = app.database.toggle_category(category_id)
        await callback.answer("Включено" if enabled else "Выключено")
        await _edit(callback, *category_page(app, category_id))

    @router.callback_query(F.data.regexp(r"^cat:times:\d+$"))
    async def category_times(callback: CallbackQuery, state: FSMContext) -> None:
        category_id = int((callback.data or "0").rsplit(":", 1)[1])
        await state.set_state(InputState.category_times)
        await state.update_data(category_id=category_id)
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                "Отправьте времена через запятую или пробел.\n"
                "Пример: <code>06:37, 10:37, 14:37, 18:37, 22:37, 02:37</code>"
            )

    @router.callback_query(F.data.regexp(r"^cat:now:\d+$"))
    async def category_now(callback: CallbackQuery) -> None:
        category_id = int((callback.data or "0").rsplit(":", 1)[1])
        await callback.answer("Запускаю поднятие…")
        try:
            await app.raise_now(category_id)
        except FunPayNotConnected:
            if callback.message:
                await callback.message.answer("FunPay ещё не подключён.")
        except KeyError:
            if callback.message:
                await callback.message.answer("Категория не найдена.")

    @router.callback_query(F.data == "noop")
    async def noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @router.message()
    async def unknown_message(message: Message) -> None:
        await message.answer(
            "Используйте /menu для открытия панели.", reply_markup=main_menu()
        )

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .application import AutomationApp
