from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from .funpay_client import IncomingMessage, MinimalFunPayClient, RateLimited

logger = logging.getLogger(__name__)


class FunPayNotConnected(RuntimeError):
    pass


class FunPayBridge:
    """Run the synchronous minimal client without blocking aiogram."""

    def __init__(self, golden_key: str, user_agent: str | None, poll_seconds: float):
        self.golden_key = golden_key
        self.user_agent = user_agent
        self.poll_seconds = poll_seconds
        self.client: MinimalFunPayClient | None = None
        self.events: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self.last_event_at: datetime | None = None
        self.last_poll_at: datetime | None = None
        self.last_error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connect_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.client is not None

    @property
    def account_id(self) -> int | None:
        return self.client.account_id if self.client else None

    @property
    def username(self) -> str | None:
        return self.client.username if self.client else None

    async def connect(self) -> None:
        async with self._connect_lock:
            if self.connected:
                return
            self._loop = asyncio.get_running_loop()
            client = MinimalFunPayClient(self.golden_key, self.user_agent)
            await asyncio.to_thread(client.authenticate)
            self.client = client
            self.last_error = None
            threading.Thread(
                target=self._listen_worker,
                daemon=True,
                name="funpay-minimal-listener",
            ).start()

    def _listen_worker(self) -> None:
        assert self.client is not None
        while True:
            started = time.monotonic()
            delay = self.poll_seconds
            try:
                messages = self.client.poll_new_messages()
                self.last_poll_at = datetime.now(timezone.utc)
                self.last_error = None
                for message in messages:
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(self._enqueue_message, message)
            except RateLimited as exc:
                delay = max(delay, exc.retry_after)
                self.last_error = str(exc)
                logger.warning(
                    "FunPay ограничил частоту запросов; пауза %.1f сек.", delay
                )
            except Exception as exc:
                delay = max(delay, 10.0)
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("Ошибка минимального клиента FunPay")
            elapsed = time.monotonic() - started
            time.sleep(max(0.2, delay - elapsed))

    def _enqueue_message(self, message: IncomingMessage) -> None:
        self.last_event_at = datetime.now(timezone.utc)
        self.events.put_nowait(message)

    async def next_event(self) -> IncomingMessage:
        return await self.events.get()

    def _require_client(self) -> MinimalFunPayClient:
        if self.client is None:
            raise FunPayNotConnected("FunPay ещё не подключён.")
        return self.client

    async def refresh_session(self) -> None:
        await asyncio.to_thread(self._require_client().refresh_session)

    async def list_lot_categories(self) -> list[tuple[int, str]]:
        return await asyncio.to_thread(self._require_client().list_lot_categories)

    async def raise_category(self, category_id: int) -> int | None:
        return await asyncio.to_thread(self._require_client().raise_lots, category_id)

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        chat_name: str | None = None,
        interlocutor_id: int | None = None,
    ) -> None:
        del chat_name, interlocutor_id
        await asyncio.to_thread(self._require_client().send_message, int(chat_id), text)

    async def is_first_user_message(
        self,
        chat_id: int | str,
        current_message_id: int,
        chat_name: str,
    ) -> bool:
        return await asyncio.to_thread(
            self._require_client().is_first_user_message,
            int(chat_id),
            current_message_id,
            chat_name,
        )
