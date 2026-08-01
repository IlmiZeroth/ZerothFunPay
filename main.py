from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from botapp.application import AutomationApp
from botapp.config import AppConfig, ConfigError
from botapp.handlers import build_dispatcher

BASE_DIR = Path(__file__).resolve().parent


async def run() -> None:
    config = AppConfig.from_env(BASE_DIR)
    app = AutomationApp(config)
    dispatcher = build_dispatcher(app)
    app.dispatcher = dispatcher
    try:
        await app.start()
        await dispatcher.start_polling(app.bot, close_bot_session=False)
    finally:
        await app.shutdown()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigError as exc:
        raise SystemExit(f"Ошибка настройки: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
