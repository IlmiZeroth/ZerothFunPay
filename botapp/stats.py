from __future__ import annotations

import io
import math
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .database import ACTIVITY_AUTO_RAISE, ACTIVITY_MESSAGE, ActivityPoint

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True, slots=True)
class MinuteSeries:
    raises: tuple[float, ...]
    messages: tuple[float, ...]
    days: int
    first_day: date | None
    last_day: date | None


@dataclass(frozen=True, slots=True)
class ActivitySummary:
    raises: int
    messages: int
    messages_within_two_hours: int
    peak_message_hour: int | None

    @property
    def messages_per_raise(self) -> float | None:
        return self.messages / self.raises if self.raises else None

    @property
    def after_raise_percent(self) -> float | None:
        if not self.messages:
            return None
        return self.messages_within_two_hours * 100 / self.messages


def minute_series(
    points: list[ActivityPoint],
    timezone: ZoneInfo,
    *,
    target_day: date | None = None,
    average: bool = False,
) -> MinuteSeries:
    raises = [0.0] * MINUTES_PER_DAY
    messages = [0.0] * MINUTES_PER_DAY
    local_points = [(point, point.occurred_at.astimezone(timezone)) for point in points]
    available_days = [local.date() for _, local in local_points]
    first_day = min(available_days) if available_days else None
    last_day = max(available_days) if available_days else None

    if average:
        day_count = (
            (last_day - first_day).days + 1
            if first_day is not None and last_day is not None
            else 0
        )
    else:
        day_count = 1

    for point, local in local_points:
        if target_day is not None and local.date() != target_day:
            continue
        minute = local.hour * 60 + local.minute
        if point.kind == ACTIVITY_AUTO_RAISE:
            raises[minute] += 1
        elif point.kind == ACTIVITY_MESSAGE:
            messages[minute] += 1

    if average and day_count > 0:
        raises = [value / day_count for value in raises]
        messages = [value / day_count for value in messages]

    return MinuteSeries(
        raises=tuple(raises),
        messages=tuple(messages),
        days=day_count,
        first_day=first_day,
        last_day=last_day,
    )


def summarize(
    points: list[ActivityPoint],
    timezone: ZoneInfo,
    *,
    target_day: date | None = None,
) -> ActivitySummary:
    all_raises = sorted(
        point.occurred_at.timestamp()
        for point in points
        if point.kind == ACTIVITY_AUTO_RAISE
    )
    visible = [
        point
        for point in points
        if target_day is None
        or point.occurred_at.astimezone(timezone).date() == target_day
    ]
    visible_raises = [point for point in visible if point.kind == ACTIVITY_AUTO_RAISE]
    messages = [point for point in visible if point.kind == ACTIVITY_MESSAGE]
    after_raise = 0
    for message in messages:
        timestamp = message.occurred_at.timestamp()
        index = bisect_right(all_raises, timestamp) - 1
        if index >= 0 and timestamp - all_raises[index] <= 2 * 60 * 60:
            after_raise += 1

    hourly = [0] * 24
    for message in messages:
        hourly[message.occurred_at.astimezone(timezone).hour] += 1
    peak_hour = max(range(24), key=hourly.__getitem__) if messages else None
    return ActivitySummary(
        raises=len(visible_raises),
        messages=len(messages),
        messages_within_two_hours=after_raise,
        peak_message_hour=peak_hour,
    )


def render_minute_chart(
    series: MinuteSeries,
    *,
    title: str,
    average: bool,
) -> bytes:
    width, height = 1400, 760
    left, top, right, bottom = 90, 105, 45, 90
    plot_width = width - left - right
    plot_height = height - top - bottom
    background = "#101722"
    grid = "#2A3545"
    text = "#D7DFEA"
    raises_color = "#55A7FF"
    messages_color = "#FF9D4D"

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=34)
    label_font = ImageFont.load_default(size=22)
    small_font = ImageFont.load_default(size=18)

    maximum = max((*series.raises, *series.messages), default=0.0)
    if maximum <= 0:
        y_max = 1.0
    elif average and maximum < 1:
        y_max = max(0.1, math.ceil(maximum * 10) / 10)
    else:
        y_max = float(math.ceil(maximum))

    draw.text((left, 30), title, fill=text, font=title_font)
    legend_y = 72
    draw.line((left, legend_y, left + 40, legend_y), fill=raises_color, width=5)
    draw.text((left + 50, legend_y - 13), "Auto raises", fill=text, font=label_font)
    draw.line(
        (left + 235, legend_y, left + 275, legend_y),
        fill=messages_color,
        width=5,
    )
    draw.text((left + 285, legend_y - 13), "Messages", fill=text, font=label_font)

    for step in range(6):
        fraction = step / 5
        y = top + plot_height - fraction * plot_height
        draw.line((left, y, left + plot_width, y), fill=grid, width=1)
        value = y_max * fraction
        label = f"{value:.2f}" if average and y_max <= 1 else f"{value:g}"
        draw.text((18, y - 10), label, fill=text, font=small_font)

    for hour in range(0, 25, 3):
        minute = min(hour * 60, MINUTES_PER_DAY - 1)
        x = left + minute / (MINUTES_PER_DAY - 1) * plot_width
        draw.line((x, top, x, top + plot_height), fill=grid, width=1)
        label = f"{hour % 24:02d}:00"
        box = draw.textbbox((0, 0), label, font=small_font)
        label_width = box[2] - box[0]
        draw.text(
            (x - label_width / 2, top + plot_height + 22),
            label,
            fill=text,
            font=small_font,
        )

    def coordinates(values: tuple[float, ...]) -> list[tuple[float, float]]:
        return [
            (
                left + index / (MINUTES_PER_DAY - 1) * plot_width,
                top + plot_height - value / y_max * plot_height,
            )
            for index, value in enumerate(values)
        ]

    draw.line(coordinates(series.raises), fill=raises_color, width=4, joint="curve")
    draw.line(coordinates(series.messages), fill=messages_color, width=4, joint="curve")
    draw.rectangle(
        (left, top, left + plot_width, top + plot_height), outline="#526176", width=2
    )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
