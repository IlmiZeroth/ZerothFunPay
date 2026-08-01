from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://funpay.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)
_CATEGORY_PATH_RE = re.compile(r"^/users/\d+/$")
_LOT_LINK_RE = re.compile(r"/lots/(\d+)/")
_USER_LINK_RE = re.compile(r"/users/(\d+)/")
_PRIVATE_CHAT_RE = re.compile(r"^users-(\d+)-(\d+)$")
_BOT_MARKERS = ("\u2061", "\u2064")


class FunPayClientError(RuntimeError):
    pass


class NetworkPolicyError(FunPayClientError):
    pass


class AuthenticationError(FunPayClientError):
    pass


class ProtocolError(FunPayClientError):
    pass


class RequestError(FunPayClientError):
    def __init__(self, operation: str, status_code: int):
        self.operation = operation
        self.status_code = status_code
        super().__init__(f"FunPay вернул HTTP {status_code} для операции {operation}.")


class RateLimited(RequestError):
    def __init__(self, operation: str, retry_after: float):
        self.retry_after = max(1.0, retry_after)
        super().__init__(operation, 429)


class RaiseLotsError(FunPayClientError):
    def __init__(self, error_message: str | None, wait_time: int | None):
        self.error_message = error_message or "FunPay отклонил поднятие лотов."
        self.wait_time = wait_time
        super().__init__(self.error_message)


class MessageSendError(FunPayClientError):
    pass


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    id: int
    name: str
    subcategory_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ChatBookmark:
    id: int
    name: str
    node_message_id: int


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    id: int
    text: str | None
    chat_id: int
    chat_name: str | None
    interlocutor_id: int | None
    author: str | None
    author_id: int
    image_link: str | None = None
    by_bot: bool = False
    by_vertex: bool = False


class MinimalFunPayClient:
    """Small, auditable FunPay session client for messages and lot raising only."""

    def __init__(
        self,
        golden_key: str,
        user_agent: str | None = None,
        timeout: float = 15.0,
    ):
        self._golden_key = golden_key
        self._timeout = timeout
        self._lock = threading.RLock()
        self._session = requests.Session()
        # Do not leak the session through HTTP(S)_PROXY or credentials from .netrc.
        self._session.trust_env = False
        self._session.headers.update(
            {
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            }
        )
        self._session.cookies.set(
            "golden_key", golden_key, domain=".funpay.com", path="/"
        )
        self._session.cookies.set("cookie_prefs", "1", domain=".funpay.com", path="/")

        self.account_id: int | None = None
        self.username: str | None = None
        self.csrf_token: str | None = None
        self.categories: dict[int, CategoryInfo] = {}
        self._subcategory_to_category: dict[int, int] = {}
        self._bookmark_tag = uuid.uuid4().hex[:8]
        self._chat_last_ids: dict[int, int] = {}
        self._pending_bookmarks: dict[int, ChatBookmark] = {}
        self._pending_attempts: dict[int, int] = {}
        self._poll_initialized = False
        self.last_success_at: datetime | None = None

    @staticmethod
    def _strip_locale(path: str) -> str:
        for prefix in ("/en", "/uk"):
            if path == prefix or path == prefix + "/":
                return "/"
            if path.startswith(prefix + "/"):
                return path[len(prefix) :]
        return path

    @classmethod
    def _validate_path(cls, path: str) -> None:
        clean_path = cls._strip_locale(path)
        allowed = clean_path in {"/", "/runner/", "/lots/raise"}
        if not allowed and not _CATEGORY_PATH_RE.fullmatch(clean_path):
            raise NetworkPolicyError(f"Путь FunPay запрещён сетевой политикой: {path}")

    @classmethod
    def _build_url(cls, path: str) -> str:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise NetworkPolicyError(f"Некорректный путь FunPay: {path}")
        cls._validate_path(path)
        return BASE_URL + path

    @staticmethod
    def _same_funpay_origin(url: str) -> bool:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "funpay.com"
            and parsed.port in (None, 443)
        )

    @staticmethod
    def _normalize_media_url(value: str | None) -> str | None:
        if not value:
            return None
        url = urljoin(BASE_URL + "/", value)
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        allowed_host = hostname in {"funpay.com", "sfunpay.com"} or hostname.endswith(
            (".funpay.com", ".sfunpay.com")
        )
        if parsed.scheme != "https" or not allowed_host:
            return None
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
    ) -> requests.Response:
        url = self._build_url(path)
        request_method = method.upper()
        request_data = data

        for _ in range(5):
            response = self._session.request(
                request_method,
                url,
                headers=headers,
                data=request_data,
                timeout=self._timeout,
                allow_redirects=False,
            )
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5.0
                raise RateLimited(operation, retry_after)
            if response.status_code == 403:
                raise AuthenticationError(
                    "FunPay отклонил сессию (HTTP 403). Проверьте golden_key."
                )
            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                if not location:
                    raise RequestError(operation, response.status_code)
                next_url = urljoin(url, location)
                if not self._same_funpay_origin(next_url):
                    raise NetworkPolicyError(
                        "FunPay попытался перенаправить запрос на другой домен."
                    )
                next_path = urlsplit(next_url).path
                if next_path.rstrip("/").endswith("account/login"):
                    raise AuthenticationError(
                        "Сессия FunPay недействительна. Проверьте golden_key."
                    )
                self._validate_path(next_path)
                url = next_url
                if response.status_code in (301, 302, 303):
                    request_method = "GET"
                    request_data = None
                continue
            if response.status_code != 200:
                raise RequestError(operation, response.status_code)
            self.last_success_at = datetime.now(timezone.utc)
            return response
        raise ProtocolError(f"Слишком много перенаправлений при операции {operation}.")

    @staticmethod
    def _soup(response: requests.Response) -> BeautifulSoup:
        return BeautifulSoup(response.content, "lxml")

    @staticmethod
    def _read_app_data(soup: BeautifulSoup) -> dict[str, Any]:
        body = soup.find("body")
        if not isinstance(body, Tag) or not body.get("data-app-data"):
            raise AuthenticationError(
                "FunPay не вернул данные авторизованного аккаунта."
            )
        try:
            data = json.loads(str(body["data-app-data"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProtocolError(
                "Не удалось разобрать data-app-data на странице FunPay."
            ) from exc
        if not data.get("userId") or not data.get("csrf-token"):
            raise AuthenticationError(
                "В сессии FunPay отсутствуют userId или CSRF-токен."
            )
        return data

    def authenticate(self) -> None:
        with self._lock:
            response = self._request("GET", "/", operation="авторизация")
            soup = self._soup(response)
            app_data = self._read_app_data(soup)
            name = soup.select_one("div.user-link-name")
            if not name:
                raise AuthenticationError("FunPay не подтвердил авторизацию аккаунта.")
            self.account_id = int(app_data["userId"])
            self.csrf_token = str(app_data["csrf-token"])
            self.username = name.get_text(strip=True)
            self._parse_catalog(soup)

    def refresh_session(self) -> None:
        self.authenticate()

    def _parse_catalog(self, soup: BeautifulSoup) -> None:
        raw_categories: dict[int, tuple[str, set[int]]] = {}
        for item in soup.select("div.promo-game-item"):
            title = item.select_one("div.game-title[data-id]")
            if not isinstance(title, Tag):
                continue
            try:
                default_id = int(str(title["data-id"]))
            except (KeyError, TypeError, ValueError):
                continue
            title_link = title.find("a")
            default_name = (
                title_link.get_text(strip=True)
                if isinstance(title_link, Tag)
                else str(default_id)
            )
            names = {default_id: default_name}
            group = item.find("div", attrs={"role": "group"})
            if isinstance(group, Tag):
                for button in group.find_all("button", attrs={"data-id": True}):
                    try:
                        regional_id = int(str(button["data-id"]))
                    except (TypeError, ValueError):
                        continue
                    names[regional_id] = (
                        f"{default_name} ({button.get_text(strip=True)})"
                    )

            for section_list in item.select("ul.list-inline[data-id]"):
                try:
                    category_id = int(str(section_list["data-id"]))
                except (KeyError, TypeError, ValueError):
                    continue
                category_name = names.get(category_id, default_name)
                existing_name, subcategories = raw_categories.setdefault(
                    category_id, (category_name, set())
                )
                for link in section_list.find_all("a", href=True):
                    match = _LOT_LINK_RE.search(str(link["href"]))
                    if match:
                        subcategories.add(int(match.group(1)))
                raw_categories[category_id] = (existing_name, subcategories)

        self.categories = {
            category_id: CategoryInfo(category_id, name, tuple(sorted(subcategories)))
            for category_id, (name, subcategories) in raw_categories.items()
            if subcategories
        }
        self._subcategory_to_category = {
            subcategory_id: category_id
            for category_id, category in self.categories.items()
            for subcategory_id in category.subcategory_ids
        }

    def list_lot_categories(self) -> list[tuple[int, str]]:
        with self._lock:
            if self.account_id is None:
                raise AuthenticationError("Клиент FunPay не авторизован.")
            response = self._request(
                "GET",
                f"/users/{self.account_id}/",
                operation="получение категорий лотов",
            )
            soup = self._soup(response)
            self._read_app_data(soup)
            active_ids: set[int] = set()
            for title in soup.select("div.offer-list-title-container a[href]"):
                match = _LOT_LINK_RE.search(str(title.get("href", "")))
                if not match:
                    continue
                category_id = self._subcategory_to_category.get(int(match.group(1)))
                if category_id is not None:
                    active_ids.add(category_id)
            return sorted(
                (
                    (category_id, self.categories[category_id].name)
                    for category_id in active_ids
                ),
                key=lambda item: item[1].casefold(),
            )

    def _runner(
        self, objects: list[dict[str, Any]], request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self.csrf_token:
            raise AuthenticationError("Клиент FunPay не авторизован.")
        payload: dict[str, Any] = {
            "csrf_token": self.csrf_token,
            "objects": json.dumps(objects, ensure_ascii=False, separators=(",", ":")),
            "request": json.dumps(request, ensure_ascii=False, separators=(",", ":"))
            if request
            else False,
        }
        response = self._request(
            "POST",
            "/runner/",
            operation="обмен сообщениями",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            data=payload,
        )
        try:
            result = response.json()
        except requests.JSONDecodeError as exc:
            raise ProtocolError("FunPay runner вернул не JSON.") from exc
        if not isinstance(result, dict) or not isinstance(result.get("objects"), list):
            raise ProtocolError("FunPay runner вернул неизвестную структуру.")
        return result

    @staticmethod
    def _parse_bookmarks(html_text: str) -> list[ChatBookmark]:
        soup = BeautifulSoup(html_text, "lxml")
        result: list[ChatBookmark] = []
        for item in soup.select("a.contact-item[data-id]"):
            message_block = item.select_one("div.contact-item-message")
            name_block = item.select_one("div.media-user-name")
            if not message_block or not name_block:
                continue
            try:
                chat_id = int(str(item["data-id"]))
                node_message_id = int(str(item.get("data-node-msg", "0")))
            except (TypeError, ValueError):
                continue
            if node_message_id <= 0:
                continue
            result.append(
                ChatBookmark(
                    id=chat_id,
                    name=name_block.get_text(strip=True),
                    node_message_id=node_message_id,
                )
            )
        return result

    def _fetch_chat_messages(
        self, bookmark: ChatBookmark, last_message_id: int = -1
    ) -> list[IncomingMessage]:
        chat_object = {
            "type": "chat_node",
            "id": bookmark.id,
            "tag": "00000000",
            "data": {
                "node": bookmark.id,
                "last_message": last_message_id,
                "content": "",
            },
        }
        result = self._runner([chat_object])
        for obj in result["objects"]:
            if obj.get("type") == "chat_node" and obj.get("data"):
                return self._parse_chat_object(obj, bookmark)
        return []

    def _parse_chat_object(
        self, obj: dict[str, Any], bookmark: ChatBookmark
    ) -> list[IncomingMessage]:
        if self.account_id is None:
            raise AuthenticationError("Клиент FunPay не авторизован.")
        data = obj.get("data") or {}
        node = data.get("node") or {}
        if node.get("silent"):
            return []
        try:
            chat_id = int(node.get("id", bookmark.id))
        except (TypeError, ValueError):
            chat_id = bookmark.id

        interlocutor_id: int | None = None
        node_name = str(node.get("name", ""))
        match = _PRIVATE_CHAT_RE.fullmatch(node_name)
        if match:
            participants = {int(match.group(1)), int(match.group(2))}
            participants.discard(self.account_id)
            interlocutor_id = next(iter(participants), None)

        known_names: dict[int, str] = {
            0: "FunPay",
            self.account_id: self.username or "Я",
        }
        if interlocutor_id is not None:
            known_names[interlocutor_id] = bookmark.name

        messages: list[IncomingMessage] = []
        for raw in data.get("messages") or []:
            try:
                message_id = int(raw["id"])
                author_id = int(raw["author"])
                html_text = str(raw["html"])
            except (KeyError, TypeError, ValueError):
                continue
            soup = BeautifulSoup(html_text.replace("<br>", "\n"), "lxml")
            author_link = soup.select_one("div.media-user-name a[href]")
            if isinstance(author_link, Tag):
                author_match = _USER_LINK_RE.search(str(author_link.get("href", "")))
                if author_match:
                    known_names[int(author_match.group(1))] = author_link.get_text(
                        strip=True
                    )

            image_tag = soup.select_one("a.chat-img-link[href]")
            image_link = (
                self._normalize_media_url(str(image_tag["href"]))
                if isinstance(image_tag, Tag)
                else None
            )
            text: str | None = None
            if image_link is None:
                if author_id == 0:
                    text_block = soup.select_one("div[role='alert']")
                else:
                    text_block = soup.select_one("div.chat-msg-text")
                if isinstance(text_block, Tag):
                    text = text_block.get_text(separator="\n", strip=False)

            by_bot = False
            if author_id == self.account_id and text:
                for marker in _BOT_MARKERS:
                    if text.startswith(marker):
                        text = text[len(marker) :]
                        by_bot = True
                        break

            messages.append(
                IncomingMessage(
                    id=message_id,
                    text=text,
                    chat_id=chat_id,
                    chat_name=bookmark.name,
                    interlocutor_id=interlocutor_id,
                    author=known_names.get(author_id),
                    author_id=author_id,
                    image_link=image_link,
                    by_bot=by_bot,
                )
            )
        return sorted(messages, key=lambda message: message.id)

    def poll_new_messages(self) -> list[IncomingMessage]:
        with self._lock:
            bookmark_object = {
                "type": "chat_bookmarks",
                "id": self.account_id,
                "tag": self._bookmark_tag,
                "data": False,
            }
            result = self._runner([bookmark_object])
            bookmarks: list[ChatBookmark] = []
            for obj in result["objects"]:
                if obj.get("type") != "chat_bookmarks":
                    continue
                if obj.get("tag"):
                    self._bookmark_tag = str(obj["tag"])
                data = obj.get("data")
                if isinstance(data, dict) and data.get("html"):
                    bookmarks.extend(self._parse_bookmarks(str(data["html"])))

            if not self._poll_initialized:
                for bookmark in bookmarks:
                    self._chat_last_ids[bookmark.id] = bookmark.node_message_id
                self._poll_initialized = True
                return []

            for bookmark in bookmarks:
                previous_id = self._chat_last_ids.get(bookmark.id)
                if previous_id is not None and bookmark.node_message_id <= previous_id:
                    continue
                pending = self._pending_bookmarks.get(bookmark.id)
                if (
                    pending is None
                    or bookmark.node_message_id > pending.node_message_id
                ):
                    self._pending_bookmarks[bookmark.id] = bookmark

            new_messages: list[IncomingMessage] = []
            for bookmark in list(self._pending_bookmarks.values()):
                previous_id = self._chat_last_ids.get(bookmark.id)
                messages = self._fetch_chat_messages(bookmark, previous_id or -1)
                if not messages:
                    attempts = self._pending_attempts.get(bookmark.id, 0) + 1
                    self._pending_attempts[bookmark.id] = attempts
                    if attempts >= 3:
                        self._chat_last_ids[bookmark.id] = bookmark.node_message_id
                        self._pending_bookmarks.pop(bookmark.id, None)
                        self._pending_attempts.pop(bookmark.id, None)
                    continue
                if previous_id is None:
                    selected = messages[-1:]
                else:
                    selected = [
                        message for message in messages if message.id > previous_id
                    ]
                new_messages.extend(selected)
                self._chat_last_ids[bookmark.id] = max(
                    [bookmark.node_message_id, *(message.id for message in messages)]
                )
                self._pending_bookmarks.pop(bookmark.id, None)
                self._pending_attempts.pop(bookmark.id, None)
            return sorted(new_messages, key=lambda message: message.id)

    def is_first_user_message(
        self, chat_id: int, current_message_id: int, chat_name: str
    ) -> bool:
        with self._lock:
            bookmark = ChatBookmark(chat_id, chat_name, current_message_id)
            messages = self._fetch_chat_messages(bookmark, -1)
            return not any(
                message.id < current_message_id
                and message.author_id not in (0, self.account_id)
                for message in messages
            )

    def send_message(self, chat_id: int, text: str) -> None:
        with self._lock:
            chat_object = {
                "type": "chat_node",
                "id": chat_id,
                "tag": "00000000",
                "data": {"node": chat_id, "last_message": -1, "content": ""},
            }
            request = {
                "action": "chat_message",
                "data": {"node": chat_id, "last_message": -1, "content": text},
            }
            result = self._runner([chat_object], request)
            response = result.get("response")
            if not isinstance(response, dict):
                raise MessageSendError("FunPay не подтвердил отправку сообщения.")
            if response.get("error"):
                raise MessageSendError(str(response["error"]))

    def raise_lots(self, category_id: int) -> int | None:
        with self._lock:
            category = self.categories.get(category_id)
            if category is None or not category.subcategory_ids:
                raise RaiseLotsError("Категория отсутствует в каталоге FunPay.", None)
            payload: dict[str, Any] = {
                "game_id": category.id,
                "node_id": category.subcategory_ids[0],
                "node_ids[]": list(category.subcategory_ids),
            }
            response = self._request(
                "POST",
                "/lots/raise",
                operation="поднятие лотов",
                headers={
                    "Accept": "*/*",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                },
                data=payload,
            )
            try:
                result = response.json()
            except requests.JSONDecodeError as exc:
                raise ProtocolError(
                    "FunPay вернул не JSON при поднятии лотов."
                ) from exc
            wait_value = result.get("wait")
            try:
                wait_time = int(wait_value) if wait_value is not None else None
            except (TypeError, ValueError):
                wait_time = None
            if not result.get("error") and not result.get("url"):
                return wait_time
            raise RaiseLotsError(result.get("msg") or result.get("url"), wait_time)
