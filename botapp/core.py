from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, time, timedelta

DEFAULT_RAISE_TIMES = ("06:37", "10:37", "14:37", "18:37", "22:37", "02:37")
_TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")


def parse_daily_times(value: str | Iterable[str]) -> list[str]:
    """Parse and normalize a comma/space separated list of HH:MM values."""
    if isinstance(value, str):
        raw_values = [item for item in re.split(r"[,;\s]+", value.strip()) if item]
    else:
        raw_values = [str(item).strip() for item in value if str(item).strip()]

    if not raw_values:
        raise ValueError("Укажите хотя бы одно время в формате ЧЧ:ММ.")

    result: list[str] = []
    for raw in raw_values:
        match = _TIME_RE.fullmatch(raw)
        if not match:
            raise ValueError(f"Некорректное время: {raw}. Нужен формат ЧЧ:ММ.")
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if hour > 23 or minute > 59:
            raise ValueError(f"Некорректное время: {raw}.")
        normalized = f"{hour:02d}:{minute:02d}"
        if normalized not in result:
            result.append(normalized)
    return result


def slot_key(now: datetime) -> str:
    """Return a durable once-per-day/minute scheduler key."""
    return now.strftime("%Y-%m-%d %H:%M")


def next_scheduled_at(
    times: Iterable[str],
    now: datetime,
    offsets_seconds: Mapping[str, float] | None = None,
) -> tuple[datetime, float]:
    """Return the next daily slot and its accumulated queue offset."""
    offsets = offsets_seconds or {}
    candidates: list[tuple[datetime, float]] = []
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for value in times:
            hour, minute = (int(part) for part in value.split(":"))
            offset = max(0.0, float(offsets.get(value, 0.0)))
            candidate = datetime.combine(
                day,
                time(hour=hour, minute=minute),
                tzinfo=now.tzinfo,
            ) + timedelta(seconds=offset)
            if candidate > now:
                candidates.append((candidate, offset))
    if not candidates:
        raise ValueError("Расписание не содержит будущего времени.")
    return min(candidates, key=lambda item: item[0])


def shorten(value: str | None, limit: int = 120) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"
