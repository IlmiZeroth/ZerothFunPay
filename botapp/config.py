from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppConfig:
    telegram_token: str
    admin_id: int
    funpay_golden_key: str
    funpay_user_agent: str | None
    timezone_name: str
    timezone: ZoneInfo
    database_path: Path
    funpay_poll_seconds: float = 4.0

    @classmethod
    def from_env(cls, base_dir: Path) -> AppConfig:
        load_dotenv(base_dir / ".env")

        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        golden_key = os.getenv("FUNPAY_GOLDEN_KEY", "").strip()
        admin_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
        timezone_name = (
            os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"
        )
        user_agent = os.getenv("FUNPAY_USER_AGENT", "").strip() or None

        missing = []
        if not telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not golden_key:
            missing.append("FUNPAY_GOLDEN_KEY")
        if not admin_raw:
            missing.append("ADMIN_TELEGRAM_ID")
        if missing:
            raise ConfigError("Не заполнены параметры в .env: " + ", ".join(missing))

        try:
            admin_id = int(admin_raw)
        except ValueError as exc:
            raise ConfigError("ADMIN_TELEGRAM_ID должен быть целым числом.") from exc
        if admin_id <= 0:
            raise ConfigError("ADMIN_TELEGRAM_ID должен быть положительным числом.")

        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ConfigError(
                f"Неизвестный часовой пояс TIMEZONE={timezone_name!r}."
            ) from exc

        poll_raw = os.getenv("FUNPAY_POLL_SECONDS", "4").strip()
        try:
            poll_seconds = max(2.0, float(poll_raw))
        except ValueError as exc:
            raise ConfigError("FUNPAY_POLL_SECONDS должен быть числом.") from exc

        db_raw = os.getenv("DATABASE_PATH", "data/bot.sqlite3").strip()
        database_path = Path(db_raw)
        if not database_path.is_absolute():
            database_path = base_dir / database_path

        return cls(
            telegram_token=telegram_token,
            admin_id=admin_id,
            funpay_golden_key=golden_key,
            funpay_user_agent=user_agent,
            timezone_name=timezone_name,
            timezone=timezone,
            database_path=database_path,
            funpay_poll_seconds=poll_seconds,
        )
