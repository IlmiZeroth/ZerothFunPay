from __future__ import annotations

import asyncio
import html
import math
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from .core import next_scheduled_at, parse_daily_times, shorten
from .database import CategorySchedule
from .funpay_bridge import FunPayNotConnected
from .stats import ActivitySummary, minute_series, render_minute_chart, summarize


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
            [
                _button("📊 Статус бота", "status"),
                _button("📈 Подъёмы", "raisestatus:0"),
            ],
            [_button("🗂 Категории", "cats:0"), _button("📉 Статистика", "stats")],
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


def _duration(seconds: float) -> str:
    remaining = max(0, math.ceil(seconds))
    if remaining < 60:
        return f"{remaining} сек."
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    if not parts and seconds:
        parts.append(f"{seconds} сек.")
    return " ".join(parts[:2])


def _category_queue_offsets(
    categories: list[CategorySchedule], delay_seconds: float
) -> dict[int, dict[str, float]]:
    """Mirror scheduler order and accumulate delay separately for every time slot."""
    positions: dict[str, int] = {}
    result: dict[int, dict[str, float]] = {}
    for category in categories:
        if not category.enabled:
            continue
        offsets: dict[str, float] = {}
        for value in category.times:
            offsets[value] = positions.get(value, 0) * delay_seconds
            positions[value] = positions.get(value, 0) + 1
        result[category.category_id] = offsets
    return result


def _ago(value: datetime, now: datetime) -> str:
    local = value.astimezone(now.tzinfo)
    return _duration((now - local).total_seconds()) + " назад"


def _category_raise_status(
    app: AutomationApp,
    category: CategorySchedule,
    categories: list[CategorySchedule],
    now: datetime,
) -> str:
    lines: list[str] = []
    if category.last_attempt_at is None:
        lines.append("⚪ Последняя попытка: ещё не было")
    elif category.last_error:
        lines.append(
            f"🔴 Последняя попытка: ошибка, {_ago(category.last_attempt_at, now)}"
        )
        lines.append(
            f"Ошибка: <code>{html.escape(shorten(category.last_error, 180))}</code>"
        )
        if category.last_success_at:
            lines.append(f"Последний успех: {_ago(category.last_success_at, now)}")
        else:
            lines.append("Последний успех: ещё не было")
    else:
        lines.append(f"🟢 Последний подъём: {_ago(category.last_attempt_at, now)}")

    if not category.enabled:
        lines.append("Следующий: отключён")
    else:
        offsets = _category_queue_offsets(
            categories, app.config.category_raise_delay_seconds
        ).get(category.category_id, {})
        next_at, queue_offset = next_scheduled_at(category.times, now, offsets)
        queue_text = (
            f", включая +{_duration(queue_offset)} очереди" if queue_offset else ""
        )
        lines.append(
            f"Следующий: через {_duration((next_at - now).total_seconds())} "
            f"(<code>{next_at.strftime('%d.%m %H:%M:%S')}</code>{queue_text})"
        )

    if category.retry_at:
        retry_at = category.retry_at.astimezone(now.tzinfo)
        if not category.enabled:
            retry_text = "приостановлен — категория выключена"
        elif retry_at > now:
            retry_text = (
                f"через {_duration((retry_at - now).total_seconds())} "
                f"(<code>{retry_at.strftime('%d.%m %H:%M:%S')}</code>)"
            )
        elif (
            category.retry_claimed_until
            and category.retry_claimed_until.astimezone(now.tzinfo) > now
        ):
            retry_text = "уже поставлен в очередь"
        else:
            retry_text = "готов к запуску"
        lines.append(
            f"Автоповтор: <b>{retry_text}</b> "
            f"(ошибок подряд: {category.retry_attempts})"
        )

    if category.next_allowed_at:
        allowed_at = category.next_allowed_at.astimezone(now.tzinfo)
        if allowed_at > now:
            lines.append(
                "Ограничение FunPay: ещё "
                f"{_duration((allowed_at - now).total_seconds())}"
            )
        else:
            lines.append("Ограничение FunPay: уже снято")
    else:
        lines.append("Ограничение FunPay: нет данных")
    return "\n".join(lines)


def raise_status_page(
    app: AutomationApp, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    page_size = 5
    categories = app.database.list_categories()
    total_pages = max(1, math.ceil(len(categories) / page_size))
    page = min(max(page, 0), total_pages - 1)
    start = page * page_size
    now = datetime.now(app.config.timezone)
    blocks = [
        f"<b>{html.escape(category.name)}</b>\n"
        + _category_raise_status(app, category, categories, now)
        for category in categories[start : start + page_size]
    ]
    body = (
        "<b>Статус подъёмов категорий</b>\n\n"
        f"Пауза между категориями: <b>{app.config.category_raise_delay_seconds:g} сек.</b> "
        "Минимальный накопленный сдвиг очереди уже включён в расчёт следующего запуска."
    )
    if blocks:
        body += "\n\n" + "\n\n".join(blocks)
    else:
        body += "\n\nКатегории ещё не загружены."

    rows: list[list[InlineKeyboardButton]] = []
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(_button("◀️", f"raisestatus:{page - 1}"))
        nav.append(_button(f"{page + 1}/{total_pages}", "noop"))
        if page + 1 < total_pages:
            nav.append(_button("▶️", f"raisestatus:{page + 1}"))
        rows.append(nav)
    rows.append([_button("🔄 Обновить", f"raisestatus:{page}")])
    rows.append([_button("🗂 Настроить категории", "cats:0")])
    rows.append(back_menu())
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


def _summary_text(summary: ActivitySummary) -> str:
    ratio = (
        f"{summary.messages_per_raise:.2f}"
        if summary.messages_per_raise is not None
        else "нет данных"
    )
    after_raise = (
        f"{summary.messages_within_two_hours} ({summary.after_raise_percent:.1f}%)"
        if summary.after_raise_percent is not None
        else "нет данных"
    )
    peak = (
        f"{summary.peak_message_hour:02d}:00–{summary.peak_message_hour:02d}:59"
        if summary.peak_message_hour is not None
        else "нет данных"
    )
    return (
        f"Автоподъёмы: <b>{summary.raises}</b>\n"
        f"Входящие сообщения: <b>{summary.messages}</b>\n"
        f"Сообщений на один подъём: <b>{ratio}</b>\n"
        f"Сообщений в первые 2 часа после подъёма: <b>{after_raise}</b>\n"
        f"Самый активный час сообщений: <b>{peak}</b>"
    )


def statistics_page(app: AutomationApp) -> tuple[str, InlineKeyboardMarkup]:
    points = app.database.list_activity_points()
    summary = summarize(points, app.config.timezone)
    local_dates = [
        point.occurred_at.astimezone(app.config.timezone).date() for point in points
    ]
    period = (
        f"{min(local_dates).strftime('%d.%m.%Y')} — {max(local_dates).strftime('%d.%m.%Y')}"
        if local_dates
        else "данных пока нет"
    )
    body = (
        "<b>Статистика активности</b>\n\n"
        f"Период: <b>{period}</b>\n"
        f"{_summary_text(summary)}\n\n"
        "В базе хранятся только тип события и точное время до секунды — без текста "
        "сообщений. Суточные и средние графики строятся с шагом в одну минуту.\n\n"
        "Связь «после подъёма» показывает совпадение по времени, а не доказывает, "
        "что сообщение пришло именно из-за подъёма."
    )
    today = datetime.now(app.config.timezone).date()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("📅 Сегодня по минутам", f"statsday:{today.isoformat()}")],
            [
                _button(
                    "Вчера",
                    f"statsday:{(today - timedelta(days=1)).isoformat()}",
                ),
                _button("📊 Средний день", "statsavg"),
            ],
            back_menu(),
        ]
    )
    return body, keyboard


def _day_bounds(day: date, app: AutomationApp) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=app.config.timezone)
    end = datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=app.config.timezone
    )
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _daily_chart_data(
    app: AutomationApp, day: date
) -> tuple[bytes, str, InlineKeyboardMarkup]:
    since, until = _day_bounds(day, app)
    points = app.database.list_activity_points(since=since, until=until)
    context_points = app.database.list_activity_points(
        since=since - timedelta(hours=2), until=until
    )
    summary = summarize(context_points, app.config.timezone, target_day=day)
    series = minute_series(points, app.config.timezone, target_day=day)
    png = render_minute_chart(
        series,
        title=f"Activity on {day.strftime('%d.%m.%Y')} (1-minute bins)",
        average=False,
    )
    caption = (
        f"<b>Статистика за {day.strftime('%d.%m.%Y')}</b>\n\n"
        f"{_summary_text(summary)}\n\n"
        "Синяя линия — успешные автоподъёмы, оранжевая — входящие сообщения. "
        "Каждая точка соответствует одной минуте."
    )
    today = datetime.now(app.config.timezone).date()
    navigation = [
        _button("◀️ День", f"statsday:{(day - timedelta(days=1)).isoformat()}")
    ]
    if day < today:
        navigation.append(
            _button("День ▶️", f"statsday:{(day + timedelta(days=1)).isoformat()}")
        )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            navigation,
            [_button("Сегодня", f"statsday:{today.isoformat()}")],
            [_button("📊 Средний день", "statsavg")],
            [_button("⬅️ К статистике", "stats")],
        ]
    )
    return png, caption, keyboard


def _average_chart_data(app: AutomationApp) -> tuple[bytes, str, InlineKeyboardMarkup]:
    points = app.database.list_activity_points()
    series = minute_series(points, app.config.timezone, average=True)
    summary = summarize(points, app.config.timezone)
    png = render_minute_chart(
        series,
        title=f"Average day across {series.days} calendar day(s) (1-minute bins)",
        average=True,
    )
    if series.first_day and series.last_day:
        period = (
            f"{series.first_day.strftime('%d.%m.%Y')} — "
            f"{series.last_day.strftime('%d.%m.%Y')}"
        )
    else:
        period = "данных пока нет"
    caption = (
        "<b>Средний суточный график за всё время</b>\n\n"
        f"Период: <b>{period}</b>\n"
        f"Календарных дней в расчёте: <b>{series.days}</b>\n"
        f"{_summary_text(summary)}\n\n"
        "Для каждой минуты показано среднее количество событий в день."
    )
    today = datetime.now(app.config.timezone).date()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("📅 Сегодня", f"statsday:{today.isoformat()}")],
            [_button("⬅️ К статистике", "stats")],
        ]
    )
    return png, caption, keyboard


async def _show_chart(
    callback: CallbackQuery,
    png: bytes,
    caption: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    if not callback.message:
        return
    if callback.message.photo:
        try:
            await callback.message.edit_media(
                InputMediaPhoto(
                    media=BufferedInputFile(png, filename="funpay-statistics.png"),
                    caption=caption,
                    parse_mode="HTML",
                ),
                reply_markup=keyboard,
            )
            return
        except TelegramBadRequest:
            pass
    await callback.message.answer_photo(
        photo=BufferedInputFile(png, filename="funpay-statistics.png"),
        caption=caption,
        reply_markup=keyboard,
    )


def category_page(
    app: AutomationApp, category_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    category = app.database.get_category(category_id)
    if category is None:
        raise KeyError(category_id)
    categories = app.database.list_categories()
    now = datetime.now(app.config.timezone)
    body = (
        f"<b>{html.escape(category.name)}</b>\n\n"
        f"Подъём: {'🟢 включён' if category.enabled else '🔴 выключен'}\n"
        f"Расписание: <code>{' · '.join(category.times)}</code>\n"
        f"ID категории: <code>{category.category_id}</code>\n\n"
        f"{_category_raise_status(app, category, categories, now)}"
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
            [_button("📈 Все статусы", "raisestatus:0")],
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
    rows.append([_button("📈 Статус подъёмов", "raisestatus:0")])
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
    if callback.message.photo:
        await callback.message.answer(text, reply_markup=keyboard)
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

    @router.callback_query(F.data == "stats")
    async def statistics_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        await _edit(callback, *statistics_page(app))

    @router.callback_query(F.data.regexp(r"^statsday:\d{4}-\d{2}-\d{2}$"))
    async def statistics_day_callback(callback: CallbackQuery) -> None:
        await callback.answer("Строю график…")
        requested = date.fromisoformat((callback.data or "").split(":", 1)[1])
        today = datetime.now(app.config.timezone).date()
        day = min(requested, today)
        png, caption, keyboard = await asyncio.to_thread(_daily_chart_data, app, day)
        await _show_chart(callback, png, caption, keyboard)

    @router.callback_query(F.data == "statsavg")
    async def statistics_average_callback(callback: CallbackQuery) -> None:
        await callback.answer("Считаю средний день…")
        png, caption, keyboard = await asyncio.to_thread(_average_chart_data, app)
        await _show_chart(callback, png, caption, keyboard)

    @router.callback_query(F.data.regexp(r"^raisestatus:\d+$"))
    async def raise_status_callback(callback: CallbackQuery) -> None:
        page = int((callback.data or "0").split(":")[1])
        await callback.answer()
        await _edit(callback, *raise_status_page(app, page))

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
        else:
            await app.reminders.restore()
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
            await _edit(callback, *category_page(app, category_id))
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
