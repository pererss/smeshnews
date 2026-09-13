from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import load_dotenv

from preview import PreviewPoller
from preview import check_source as preview_check_source

try:
    from userbot import UserbotSource, parse_proxy_url
except ImportError:
    UserbotSource = None
    parse_proxy_url = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")

log = logging.getLogger("smesh")

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")
ROUTE_ID_RE = re.compile(r"^\d+$")

DELAY_CHOICES = (0, 1, 3, 5)
ALBUM_FLUSH_DELAY = 1.0
MAX_MEDIA_GROUP = 10
ERROR_NOTIFY_COOLDOWN = 60.0
ALLOWED_CHAT_TYPES = {"channel", "group", "supergroup"}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "mode": "forward",
    "notify_errors": True,
    "delay": 0,
    "routes": [],
}

NOT_FOUND_MESSAGE = (
    "❌ Не нашёл такой канал.\n\n"
    "Проверь:\n"
    "• правильно ли написан @username;\n"
    "• добавлен ли бот в канал;\n"
    "• есть ли у бота права администратора."
)
SOURCE_ADMIN_MESSAGE = (
    "❌ Бот не админ в этом канале.\n\n"
    "Telegram показывает боту сообщения только там, где он администратор.\n"
    "Добавь бота в канал как админа и попробуй снова."
)
DEST_ADMIN_MESSAGE = (
    "❌ Бот не админ в этом канале.\n\n"
    "Чтобы бот мог отправлять сообщения, добавь его в канал как админа."
)
DEST_POST_MESSAGE = (
    "❌ У бота нет права писать в этот канал.\n\n"
    "Открой права бота в канале и включи «Публикация сообщений»."
)
DEST_MEMBER_MESSAGE = (
    "❌ Бот не добавлен в эту группу.\n\n"
    "Добавь бота в группу и попробуй снова."
)


class AdminFilter:
    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    def __call__(self, event: Any) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id == self.admin_id


class AddRoute(StatesGroup):
    source = State()
    destination = State()
    confirm = State()


class EditRoute(StatesGroup):
    source = State()
    destination = State()


class ConfigStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.path):
            log.warning("Файл config.json не найден, создаю новый")
            data = copy.deepcopy(DEFAULT_CONFIG)
            self._data = data
            self.save()
            return data
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            backup = f"{self.path}.broken-{int(time.time())}"
            log.error("Файл config.json повреждён: %s", exc)
            try:
                os.replace(self.path, backup)
                log.error("Повреждённый config сохранён как %s", backup)
            except OSError as backup_exc:
                log.error("Не удалось сохранить резервную копию: %s", backup_exc)
            raw = {}
        data = self._normalize(raw)
        self._data = data
        self.save()
        return data

    @staticmethod
    def _normalize(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raw = {}
        mode = raw.get("mode")
        if mode not in ("forward", "copy"):
            mode = "forward"
        try:
            delay = int(raw.get("delay") or 0)
        except (TypeError, ValueError):
            delay = 0
        delay = max(0, min(delay, 60))
        data: dict[str, Any] = {
            "enabled": bool(raw.get("enabled", True)),
            "mode": mode,
            "notify_errors": bool(raw.get("notify_errors", True)),
            "delay": delay,
            "routes": [],
        }
        raw_routes = raw.get("routes")
        if not isinstance(raw_routes, list):
            raw_routes = []
        used_ids: set[str] = set()
        next_id = 1
        for item in raw_routes:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            destination = str(item.get("destination") or "").strip()
            if not source or not destination:
                continue
            route_id = str(item.get("id") or "").strip()
            if not route_id or route_id in used_ids:
                while str(next_id) in used_ids:
                    next_id += 1
                route_id = str(next_id)
            used_ids.add(route_id)
            via = item.get("via")
            if via not in ("bot", "preview", "account"):
                via = "bot"
            try:
                last_id = int(item.get("last_processed_message_id") or 0)
            except (TypeError, ValueError):
                last_id = 0
            data["routes"].append(
                {
                    "id": route_id,
                    "source": source,
                    "destination": destination,
                    "enabled": bool(item.get("enabled", True)),
                    "via": via,
                    "last_processed_message_id": max(last_id, 0),
                }
            )
        return data

    def _save_locked(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._data.get("enabled", True))

    def set_enabled(self, value: bool) -> None:
        with self._lock:
            self._data["enabled"] = bool(value)
            self._save_locked()

    @property
    def mode(self) -> str:
        with self._lock:
            mode = self._data.get("mode", "forward")
            return mode if mode in ("forward", "copy") else "forward"

    def set_mode(self, mode: str) -> None:
        if mode not in ("forward", "copy"):
            raise ValueError(f"Unknown mode: {mode}")
        with self._lock:
            self._data["mode"] = mode
            self._save_locked()

    @property
    def notify_errors(self) -> bool:
        with self._lock:
            return bool(self._data.get("notify_errors", True))

    def set_notify_errors(self, value: bool) -> None:
        with self._lock:
            self._data["notify_errors"] = bool(value)
            self._save_locked()

    @property
    def delay(self) -> int:
        with self._lock:
            try:
                return max(0, int(self._data.get("delay", 0) or 0))
            except (TypeError, ValueError):
                return 0

    def set_delay(self, value: int) -> None:
        value = int(value)
        if value < 0:
            raise ValueError(f"Unknown delay: {value}")
        with self._lock:
            self._data["delay"] = value
            self._save_locked()

    def routes(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data.get("routes", []))

    def route(self, route_id: str) -> dict[str, Any] | None:
        with self._lock:
            for route in self._data.get("routes", []):
                if str(route.get("id")) == str(route_id):
                    return copy.deepcopy(route)
        return None

    def add_route(self, source: str, destination: str, via: str = "bot") -> dict[str, Any]:
        with self._lock:
            route = {
                "id": self._next_route_id(),
                "source": source,
                "destination": destination,
                "enabled": True,
                "via": via,
                "last_processed_message_id": 0,
            }
            self._data.setdefault("routes", []).append(route)
            self._save_locked()
            return copy.deepcopy(route)

    def _next_route_id(self) -> str:
        used = {str(route.get("id", "")) for route in self._data.get("routes", [])}
        index = 1
        while str(index) in used:
            index += 1
        return str(index)

    def delete_route(self, route_id: str) -> bool:
        with self._lock:
            routes = self._data.get("routes", [])
            for index, route in enumerate(routes):
                if str(route.get("id")) == str(route_id):
                    routes.pop(index)
                    self._save_locked()
                    return True
        return False

    def update_route(self, route_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"source", "destination", "enabled", "via", "last_processed_message_id"}
        with self._lock:
            for route in self._data.get("routes", []):
                if str(route.get("id")) != str(route_id):
                    continue
                for key, value in fields.items():
                    if key in allowed:
                        route[key] = value
                self._save_locked()
                return copy.deepcopy(route)
        return None

    def set_last_processed(self, route_id: str, message_id: int) -> None:
        with self._lock:
            for route in self._data.get("routes", []):
                if str(route.get("id")) != str(route_id):
                    continue
                current = int(route.get("last_processed_message_id", 0) or 0)
                if int(message_id) > current:
                    route["last_processed_message_id"] = int(message_id)
                    self._save_locked()
                return

    def routes_for_source(self, chat_id: int, username: str | None) -> list[dict[str, Any]]:
        username_lower = (username or "").lower()
        result: list[dict[str, Any]] = []
        with self._lock:
            for route in self._data.get("routes", []):
                source = str(route.get("source", "")).strip()
                if source.startswith("@"):
                    if username_lower and username_lower == source[1:].lower():
                        result.append(copy.deepcopy(route))
                elif source.lstrip("-").isdigit() and int(source) == int(chat_id):
                    result.append(copy.deepcopy(route))
        return result

    def sources(self) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        with self._lock:
            for route in self._data.get("routes", []):
                source = str(route.get("source", ""))
                item = grouped.setdefault(source, {"source": source, "total": 0, "enabled": 0})
                item["total"] += 1
                if route.get("enabled", True):
                    item["enabled"] += 1
        return list(grouped.values())

    def set_source_enabled(self, source: str, value: bool) -> int:
        changed = 0
        with self._lock:
            for route in self._data.get("routes", []):
                if str(route.get("source", "")) == source:
                    route["enabled"] = bool(value)
                    changed += 1
            if changed:
                self._save_locked()
        return changed

    def delete_source(self, source: str) -> int:
        with self._lock:
            routes = self._data.get("routes", [])
            remaining = [route for route in routes if str(route.get("source", "")) != source]
            removed = len(routes) - len(remaining)
            if removed:
                self._data["routes"] = remaining
                self._save_locked()
            return removed

    def duplicate_exists(self, source: str, destination: str, ignore_id: str | None = None) -> bool:
        with self._lock:
            for route in self._data.get("routes", []):
                if ignore_id is not None and str(route.get("id")) == str(ignore_id):
                    continue
                if _same_target(route.get("source", ""), source) and _same_target(
                    route.get("destination", ""), destination
                ):
                    return True
        return False


def _same_target(left: Any, right: Any) -> bool:
    return str(left).strip().lower() == str(right).strip().lower()


class Stats:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.received = 0
        self.forwarded = 0
        self.errors = 0
        self.skipped = 0
        self.last_error: str | None = None

    def add_received(self) -> None:
        self.received += 1

    def add_forwarded(self, count: int = 1) -> None:
        self.forwarded += max(int(count), 0)

    def add_skipped(self, count: int = 1) -> None:
        self.skipped += max(int(count), 0)

    def add_error(self, text: str) -> None:
        self.errors += 1
        self.last_error = str(text)[:500]

    def uptime(self) -> str:
        seconds = max(int(time.time() - self.started_at), 0)
        if seconds < 60:
            return f"{seconds} сек"
        minutes = seconds // 60
        hours = minutes // 60
        minutes %= 60
        if hours:
            return f"{hours} ч {minutes} мин"
        return f"{minutes} мин"


class RecentTracker:
    def __init__(self, max_size: int = 1000, ttl: float = 600.0) -> None:
        self._max_size = max(int(max_size), 1)
        self._ttl = max(float(ttl), 1.0)
        self._items: dict[tuple[str, int], float] = {}

    def _purge(self, now: float) -> None:
        expired = [key for key, timestamp in self._items.items() if now - timestamp > self._ttl]
        for key in expired:
            self._items.pop(key, None)

    def seen(self, route_id: str, message_id: int) -> bool:
        now = time.time()
        self._purge(now)
        key = (str(route_id), int(message_id))
        if key in self._items:
            self._items[key] = now
            return True
        return False

    def add(self, route_id: str, message_id: int) -> None:
        now = time.time()
        self._purge(now)
        self._items[(str(route_id), int(message_id))] = now
        if len(self._items) > self._max_size:
            oldest = min(self._items, key=self._items.get)
            self._items.pop(oldest, None)


def normalize_channel_input(raw: str) -> str:
    value = (raw or "").strip()
    lowered = value.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            lowered = value.lower()
            break
    for domain in ("t.me/", "telegram.me/", "telegram.dog/"):
        if lowered.startswith(domain):
            value = value[len(domain):]
            break
    value = value.strip().strip("/")
    if not value:
        raise ValueError("❌ Пустое значение. Напишите @username или ID канала.")
    if value.startswith("+"):
        raise ValueError(
            "❌ Пригласительные ссылки не поддерживаются.\n\n"
            "Укажите @username канала или ID вида -1001234567890."
        )
    if value.startswith("@"):
        username = value[1:]
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("❌ Некорректный username. Например: @toporlive")
        return "@" + username
    if value.lstrip("-").isdigit():
        return value
    if USERNAME_RE.fullmatch(value):
        return "@" + value
    raise ValueError("❌ Не понимаю формат. Напишите @username или ID вида -1001234567890.")


async def validate_target(bot: Bot, bot_id: int, raw: str, role: str) -> tuple[Any, str]:
    normalized = normalize_channel_input(raw)
    target: Any = int(normalized) if normalized.lstrip("-").isdigit() else normalized
    try:
        chat = await bot.get_chat(target)
    except TelegramBadRequest as exc:
        if "chat not found" in str(exc).lower():
            raise ValueError(NOT_FOUND_MESSAGE) from exc
        raise ValueError("❌ Telegram отклонил запрос. Проверьте данные и попробуйте снова.") from exc
    except TelegramForbiddenError as exc:
        raise ValueError("❌ У бота нет доступа к этому чату. Добавьте бота и попробуйте снова.") from exc
    except TelegramAPIError as exc:
        raise ValueError("❌ Не удалось проверить чат. Попробуйте позже.") from exc
    if chat.type not in ALLOWED_CHAT_TYPES:
        raise ValueError("❌ Это не канал и не группа. Укажите @username канала или ID вида -100...")
    try:
        member = await bot.get_chat_member(chat.id, bot_id)
    except TelegramBadRequest as exc:
        raise ValueError(f"❌ Бот не добавлен в «{chat.title}». Добавьте бота и попробуйте снова.") from exc
    except TelegramForbiddenError as exc:
        raise ValueError(f"❌ Нет доступа к «{chat.title}». Проверьте права бота.") from exc
    except TelegramAPIError as exc:
        raise ValueError("❌ Не удалось проверить права бота. Попробуйте позже.") from exc
    status = getattr(member, "status", "")
    if role == "source":
        if status not in ("administrator", "creator"):
            raise ValueError(SOURCE_ADMIN_MESSAGE)
    elif chat.type == "channel":
        if status not in ("administrator", "creator"):
            raise ValueError(DEST_ADMIN_MESSAGE)
        if getattr(member, "can_post_messages", None) is False:
            raise ValueError(DEST_POST_MESSAGE)
    elif status not in ("administrator", "creator", "member"):
        raise ValueError(DEST_MEMBER_MESSAGE)
    return chat, normalized


def destination_chat_id(value: str) -> Any:
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def message_link(message: Message) -> str:
    if message.chat.username:
        url = f"https://t.me/{message.chat.username}/{message.message_id}"
        return f'<a href="{html.escape(url)}">открыть сообщение</a>'
    return f"<code>id={message.message_id}</code>"


def human_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "noforwards" in text or "protected content" in text:
        return "В канале-источнике запрещено сохранение контента, поэтому переслать нельзя."
    if "file_too_big" in text:
        return "Файл больше 50 МБ — Telegram не даёт боту отправлять такие файлы."
    if "restricted" in text or "protected" in text:
        return "Telegram не разрешил отправку этого сообщения."
    if (
        "forbidden" in text
        or "not enough rights" in text
        or "not a member" in text
        or "chat not found" in text
        or "kicked" in text
    ):
        return "У бота нет доступа к каналу, куда надо отправить. Проверь права бота."
    if "retry_after" in text or "flood" in text or "too many requests" in text:
        return "Слишком много сообщений подряд — Telegram попросил подождать."
    return "Не получилось отправить. Подробности — в логе."


class Forwarder:
    def __init__(self, bot: Bot, store: ConfigStore, stats: Stats, recent: RecentTracker, admin_id: int) -> None:
        self.bot = bot
        self.store = store
        self.stats = stats
        self.recent = recent
        self.admin_id = admin_id
        self._tasks: set[asyncio.Task] = set()
        self._buffers: dict[tuple[str, str], list[Message]] = {}
        self._album_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._last_notify: dict[str, float] = {}

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._buffers.clear()
        self._album_tasks.clear()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("Ошибка Telegram API: %s", exc)

    async def handle_incoming(self, message: Message) -> None:
        if not self.store.enabled:
            return
        sender = message.from_user
        if sender is not None and (sender.is_bot or sender.id == self.bot.id):
            return
        routes = self.store.routes_for_source(message.chat.id, message.chat.username)
        routes = [
            route
            for route in routes
            if route.get("enabled", True) and route.get("via", "bot") == "bot"
        ]
        if not routes:
            return
        self.stats.add_received()
        log.info("Получено новое сообщение: chat=%s message_id=%s", message.chat.id, message.message_id)
        for route in routes:
            if not self.mark_processed(route, message.message_id):
                continue
            if message.media_group_id and self.store.mode == "copy":
                self._buffer_album(route, message)
                continue
            self._spawn(self._deliver_one(route, message))

    def mark_processed(self, route: dict[str, Any], message_id: int) -> bool:
        route_id = str(route["id"])
        last_id = int(route.get("last_processed_message_id", 0) or 0)
        if int(message_id) <= last_id:
            self.stats.add_skipped()
            return False
        if self.recent.seen(route_id, message_id):
            self.stats.add_skipped()
            return False
        self.recent.add(route_id, message_id)
        self.store.set_last_processed(route_id, message_id)
        return True

    def destination_of(self, route: dict[str, Any]) -> Any:
        return destination_chat_id(route["destination"])

    def _buffer_album(self, route: dict[str, Any], message: Message) -> None:
        key = (str(route["id"]), str(message.media_group_id))
        self._buffers.setdefault(key, []).append(message)
        previous = self._album_tasks.pop(key, None)
        if previous is not None:
            previous.cancel()
        task = asyncio.create_task(self._flush_album(key))
        self._album_tasks[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush_album(self, key: tuple[str, str]) -> None:
        try:
            await asyncio.sleep(ALBUM_FLUSH_DELAY)
        except asyncio.CancelledError:
            return
        self._album_tasks.pop(key, None)
        messages = self._buffers.pop(key, [])
        if not messages or not self.store.enabled:
            return
        route = self.store.route(key[0])
        if route is None or not route.get("enabled", True):
            return
        messages.sort(key=lambda item: item.message_id)
        handled = await self._deliver_album_copy(route, messages)
        if handled:
            return
        for message in messages:
            await self._deliver_one(route, message)

    def _build_media_group(self, messages: list[Message]) -> list[Any] | None:
        media: list[Any] = []
        for message in messages:
            if message.photo:
                media.append(InputMediaPhoto(media=message.photo[-1].file_id, caption=message.caption))
            elif message.video:
                media.append(InputMediaVideo(media=message.video.file_id, caption=message.caption))
            elif message.document:
                media.append(InputMediaDocument(media=message.document.file_id, caption=message.caption))
            elif message.audio:
                media.append(InputMediaAudio(media=message.audio.file_id, caption=message.caption))
            else:
                return None
        return media

    async def _deliver_album_copy(self, route: dict[str, Any], messages: list[Message]) -> bool:
        media = self._build_media_group(messages)
        if media is None:
            return False
        destination = destination_chat_id(route["destination"])
        if self.store.delay:
            await asyncio.sleep(self.store.delay)
        total = len(messages)
        for start in range(0, total, MAX_MEDIA_GROUP):
            chunk_messages = messages[start:start + MAX_MEDIA_GROUP]
            chunk_media = media[start:start + MAX_MEDIA_GROUP]
            try:
                if len(chunk_media) == 1:
                    single = chunk_messages[0]
                    await self.bot.copy_message(
                        chat_id=destination,
                        from_chat_id=single.chat.id,
                        message_id=single.message_id,
                    )
                else:
                    await self.bot.send_media_group(chat_id=destination, media=chunk_media)
                self.stats.add_forwarded(len(chunk_media))
            except TelegramAPIError as exc:
                log.warning("Не удалось отправить альбом: %s", exc)
                for message in messages[start:]:
                    await self._deliver_one(route, message)
                return True
        log.info("Сообщение успешно отправлено: %s -> %s (альбом)", route["source"], route["destination"])
        return True

    async def _send(self, mode: str, route: dict[str, Any], message: Message) -> None:
        destination = destination_chat_id(route["destination"])
        if mode == "copy":
            await self.bot.copy_message(
                chat_id=destination,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        else:
            await self.bot.forward_message(
                chat_id=destination,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
        self.stats.add_forwarded()
        log.info("Сообщение успешно отправлено: %s -> %s", route["source"], route["destination"])

    async def _deliver_one(self, route: dict[str, Any], message: Message) -> None:
        if self.store.delay:
            await asyncio.sleep(self.store.delay)
        mode = self.store.mode
        try:
            await self._send(mode, route, message)
            return
        except TelegramRetryAfter as exc:
            wait = int(getattr(exc, "retry_after", 3)) + 1
            log.warning("Telegram попросил подождать %s сек", wait)
            await asyncio.sleep(wait)
            try:
                await self._send(mode, route, message)
                return
            except TelegramAPIError as retry_exc:
                await self._report_error(route, message, retry_exc)
                return
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if mode == "forward" and ("restricted" in text or "protected" in text):
                log.warning("Пересылка запрещена, пробую копирование")
                try:
                    await self._send("copy", route, message)
                    return
                except TelegramAPIError as copy_exc:
                    await self._report_error(route, message, copy_exc)
                    return
            await self._report_error(route, message, exc)
            return
        except TelegramAPIError as exc:
            await self._report_error(route, message, exc)
            return
        except Exception as exc:  # noqa: BLE001
            await self._report_error(route, message, exc)

    async def _report_error(self, route: dict[str, Any], message: Message, exc: BaseException) -> None:
        await self.report_error(route, message_link(message), exc)

    async def report_error(self, route: dict[str, Any], label: str, exc: BaseException) -> None:
        self.stats.add_error(str(exc))
        log.warning("Не удалось отправить сообщение: %s -> %s", route["source"], route["destination"])
        log.error("Ошибка Telegram API: %s", exc)
        if not self.store.notify_errors:
            return
        now = time.time()
        if now - self._last_notify.get(str(route["id"]), 0.0) < ERROR_NOTIFY_COOLDOWN:
            return
        self._last_notify[str(route["id"])] = now
        text = (
            "⚠️ Не удалось отправить сообщение.\n\n"
            f"Маршрут:\n<code>{html.escape(str(route['source']))}</code>\n↓\n"
            f"<code>{html.escape(str(route['destination']))}</code>\n\n"
            f"Сообщение: {label}\n\n"
            f"Причина:\n{html.escape(human_error(exc))}"
        )
        try:
            await self.bot.send_message(self.admin_id, text)
        except TelegramAPIError as notify_exc:
            log.error("Ошибка Telegram API: %s", notify_exc)


def panel_text(store: ConfigStore) -> str:
    routes = store.routes()
    status = "🟢 Работает" if store.enabled else "⏸ Остановлен"
    return "📰 <b>SMESH FORWARDER</b>\n\n" f"Статус: <b>{status}</b>\n" f"Маршрутов: <b>{len(routes)}</b>"


def sources_text(store: ConfigStore) -> str:
    sources = store.sources()
    if not sources:
        return "📡 <b>Источники</b>\n\nПока нет ни одного источника."
    lines = ["📡 <b>Источники</b>\n"]
    for index, item in enumerate(sources, start=1):
        mark = "🟢" if item["enabled"] else "🔴"
        lines.append(f"{index}. {mark} <code>{html.escape(item['source'])}</code>")
    return "\n".join(lines)


def source_text(store: ConfigStore, source: str) -> str:
    routes = [route for route in store.routes() if str(route.get("source")) == source]
    enabled = sum(1 for route in routes if route.get("enabled", True))
    if routes and enabled == len(routes):
        status = "🟢 Включён"
    elif enabled:
        status = "🟡 Частично включён"
    else:
        status = "🔴 Выключен"
    lines = [
        f"📡 <b>Источник</b> <code>{html.escape(source)}</code>\n",
        f"Статус: <b>{status}</b>",
        f"Маршрутов: <b>{len(routes)}</b>",
        "",
    ]
    for route in routes:
        mark = "🟢" if route.get("enabled", True) else "🔴"
        lines.append(f"{mark} → <code>{html.escape(str(route['destination']))}</code>")
    return "\n".join(lines)


def routes_text(store: ConfigStore) -> str:
    routes = store.routes()
    if not routes:
        return "🔀 <b>Маршруты</b>\n\nПока нет ни одного маршрута."
    lines = ["🔀 <b>Маршруты</b>"]
    for index, route in enumerate(routes, start=1):
        status = "🟢 Включён" if route.get("enabled", True) else "🔴 Выключен"
        lines.append(
            f"\n{index}.\n<code>{html.escape(str(route['source']))}</code>\n↓\n"
            f"<code>{html.escape(str(route['destination']))}</code>\n{status}"
        )
    return "\n".join(lines)


def route_text(route: dict[str, Any]) -> str:
    status = "🟢 Включён" if route.get("enabled", True) else "🔴 Выключен"
    via = {"bot": "бот-админ", "preview": "веб-страница", "account": "твой аккаунт"}.get(
        str(route.get("via", "bot")), "бот-админ"
    )
    return (
        f"⚙️ <b>Маршрут {html.escape(str(route['id']))}</b>\n\n"
        f"📡 Источник:\n<code>{html.escape(str(route['source']))}</code>\n\n"
        f"📢 Назначение:\n<code>{html.escape(str(route['destination']))}</code>\n\n"
        f"Читаю: <b>{via}</b>\n"
        f"Статус: <b>{status}</b>"
    )


def status_text(store: ConfigStore, stats: Stats) -> str:
    routes = store.routes()
    active = sum(1 for route in routes if route.get("enabled", True))
    forwarding = "🟢 Включена" if store.enabled else "⏸ Остановлена"
    return (
        "📊 <b>Статус</b>\n\n"
        "Бот:\n🟢 Работает\n\n"
        f"Общая пересылка:\n{forwarding}\n\n"
        f"Маршрутов:\n<b>{len(routes)}</b>\n\n"
        f"Активных:\n<b>{active}</b>\n\n"
        "За этот запуск:\n\n"
        f"Получено сообщений: <b>{stats.received}</b>\n"
        f"Успешно отправлено: <b>{stats.forwarded}</b>\n"
        f"Ошибок: <b>{stats.errors}</b>\n\n"
        f"Время работы:\n<b>{stats.uptime()}</b>"
    )


def settings_text(store: ConfigStore) -> str:
    mode = "без ссылки (копия)" if store.mode == "copy" else "со ссылкой (пересылка)"
    notify = "включены" if store.notify_errors else "выключены"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"📝 Оформление: <b>{mode}</b>\n"
        f"🔔 Уведомления об ошибках: <b>{notify}</b>\n"
        f"⏱ Задержка: <b>{store.delay} сек</b>"
    )


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📡 Источники", callback_data="menu:sources")
    builder.button(text="🔀 Маршруты", callback_data="menu:routes")
    builder.button(text="➕ Добавить маршрут", callback_data="route:add")
    builder.button(text="➖ Удалить маршрут", callback_data="menu:remove")
    builder.button(text="▶️ Запустить", callback_data="system:start")
    builder.button(text="⏸ Остановить", callback_data="system:pause")
    builder.button(text="📊 Статус", callback_data="menu:status")
    builder.button(text="⚙️ Настройки", callback_data="menu:settings")
    builder.adjust(2, 1, 1, 2, 2)
    return builder.as_markup()


def back_button(callback_data: str = "menu:main", text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=callback_data)
    return builder.as_markup()


def cancel_button() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="fsm:cancel")
    return builder.as_markup()


def sources_keyboard(store: ConfigStore) -> InlineKeyboardMarkup:
    sources = store.sources()
    builder = InlineKeyboardBuilder()
    for index, item in enumerate(sources, start=1):
        builder.button(text=f"⚙️ Настроить {index}", callback_data=f"src:open:{item['source']}")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(*([1] * len(sources)), 1)
    return builder.as_markup()


def source_keyboard(store: ConfigStore, source: str) -> InlineKeyboardMarkup:
    routes = [route for route in store.routes() if str(route.get("source")) == source]
    all_enabled = bool(routes) and all(route.get("enabled", True) for route in routes)
    builder = InlineKeyboardBuilder()
    if all_enabled:
        builder.button(text="⏸ Выключить", callback_data=f"src:toggle:{source}")
    else:
        builder.button(text="▶️ Включить", callback_data=f"src:toggle:{source}")
    builder.button(text="🗑 Удалить", callback_data=f"src:del:{source}")
    builder.button(text="⬅️ Назад", callback_data="menu:sources")
    builder.adjust(1)
    return builder.as_markup()


def confirm_source_delete_keyboard(source: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Да, удалить", callback_data=f"src:delconfirm:{source}")
    builder.button(text="⬅️ Отмена", callback_data=f"src:open:{source}")
    builder.adjust(2)
    return builder.as_markup()


def routes_keyboard(store: ConfigStore) -> InlineKeyboardMarkup:
    routes = store.routes()
    builder = InlineKeyboardBuilder()
    for index, route in enumerate(routes, start=1):
        builder.button(text=f"⚙️ Настроить {index}", callback_data=f"route:open:{route['id']}")
        if route.get("enabled", True):
            builder.button(text=f"⏸ Выключить {index}", callback_data=f"route:toggle:{route['id']}")
        else:
            builder.button(text=f"▶️ Включить {index}", callback_data=f"route:toggle:{route['id']}")
        builder.button(text=f"🗑 Удалить {index}", callback_data=f"route:del:{route['id']}")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(*([3] * len(routes)), 1)
    return builder.as_markup()


def route_keyboard(route: dict[str, Any]) -> InlineKeyboardMarkup:
    route_id = route["id"]
    builder = InlineKeyboardBuilder()
    if route.get("enabled", True):
        builder.button(text="⏸ Выключить", callback_data=f"route:vtoggle:{route_id}")
    else:
        builder.button(text="▶️ Включить", callback_data=f"route:vtoggle:{route_id}")
    builder.button(text="📡 Изменить источник", callback_data=f"route:setsrc:{route_id}")
    builder.button(text="📢 Изменить назначение", callback_data=f"route:setdst:{route_id}")
    builder.button(text="🗑 Удалить", callback_data=f"route:del:{route_id}")
    builder.button(text="⬅️ Назад", callback_data="menu:routes")
    builder.adjust(1)
    return builder.as_markup()


def confirm_route_delete_keyboard(route_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Да, удалить", callback_data=f"route:delconfirm:{route_id}")
    builder.button(text="⬅️ Отмена", callback_data=f"route:open:{route_id}")
    builder.adjust(2)
    return builder.as_markup()


def add_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Создать", callback_data="route:create")
    builder.button(text="❌ Отмена", callback_data="fsm:cancel")
    builder.adjust(2)
    return builder.as_markup()


def status_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="status:refresh")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def settings_keyboard(store: ConfigStore) -> InlineKeyboardMarkup:
    mode_label = "📝 Оформление: копия" if store.mode == "copy" else "📝 Оформление: пересылка"
    notify_label = "🔔 Ошибки: уведомлять" if store.notify_errors else "🔕 Ошибки: не уведомлять"
    builder = InlineKeyboardBuilder()
    builder.button(text=mode_label, callback_data="settings:mode")
    builder.button(text=notify_label, callback_data="settings:notify")
    builder.button(text=f"⏱ Задержка: {store.delay} сек", callback_data="settings:delay")
    builder.button(text="⬅️ Назад", callback_data="menu:main")
    builder.adjust(1)
    return builder.as_markup()


def build_admin_router(
    bot: Bot,
    store: ConfigStore,
    stats: Stats,
    admin_id: int,
    bot_id: int,
    userbot: Any = None,
    proxy_url: str = "",
) -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.type == "private", AdminFilter(admin_id))
    router.callback_query.filter(AdminFilter(admin_id))

    async def check_source(raw: str) -> tuple[str, str, str]:
        if userbot is not None:
            entity, normalized = await userbot.validate_source(raw)
            return str(getattr(entity, "title", normalized)), normalized, "account"
        try:
            chat, normalized = await validate_target(bot, bot_id, raw, "source")
            return str(chat.title), normalized, "bot"
        except ValueError as bot_error:
            try:
                title, normalized = await preview_check_source(
                    raw, normalize_channel_input, proxy_url or None
                )
                return title, normalized, "preview"
            except ValueError as preview_error:
                raise ValueError(f"{preview_error}\n\n{bot_error}") from preview_error

    async def show(callback: CallbackQuery, text: str, markup: Any) -> None:
        if callback.message is None:
            return
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                log.warning("Не удалось обновить сообщение: %s", exc)

    @router.message(CommandStart())
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(panel_text(store), reply_markup=main_menu())

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("✅ Действие отменено.", reply_markup=main_menu())

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "📰 <b>SMESH FORWARDER</b>\n\n"
            "/start — открыть панель управления\n"
            "/cancel — отменить текущее действие\n"
            "/help — эта справка\n\n"
            "Бот пересылает новые сообщения из источников в назначения.",
            reply_markup=main_menu(),
        )

    @router.callback_query(F.data == "menu:main")
    async def cb_main(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await show(callback, panel_text(store), main_menu())

    @router.callback_query(F.data == "menu:sources")
    async def cb_sources(callback: CallbackQuery) -> None:
        await callback.answer()
        await show(callback, sources_text(store), sources_keyboard(store))

    @router.callback_query(F.data == "menu:routes")
    async def cb_routes(callback: CallbackQuery) -> None:
        await callback.answer()
        await show(callback, routes_text(store), routes_keyboard(store))

    @router.callback_query(F.data == "menu:remove")
    async def cb_remove(callback: CallbackQuery) -> None:
        await callback.answer()
        routes = store.routes()
        if not routes:
            await show(callback, "➖ <b>Удалить маршрут</b>\n\nСписок маршрутов пуст.", back_button())
            return
        await show(
            callback,
            "➖ <b>Удалить маршрут</b>\n\nВыберите маршрут:",
            routes_keyboard(store),
        )

    @router.callback_query(F.data == "menu:status")
    async def cb_status(callback: CallbackQuery) -> None:
        await callback.answer()
        await show(callback, status_text(store, stats), status_keyboard())

    @router.callback_query(F.data == "status:refresh")
    async def cb_status_refresh(callback: CallbackQuery) -> None:
        await callback.answer("Обновлено")
        await show(callback, status_text(store, stats), status_keyboard())

    @router.callback_query(F.data == "menu:settings")
    async def cb_settings(callback: CallbackQuery) -> None:
        await callback.answer()
        await show(callback, settings_text(store), settings_keyboard(store))

    @router.callback_query(F.data == "settings:mode")
    async def cb_settings_mode(callback: CallbackQuery) -> None:
        new_mode = "copy" if store.mode == "forward" else "forward"
        store.set_mode(new_mode)
        log.info("Оформление изменено: %s", new_mode)
        await callback.answer("Оформление: копия" if new_mode == "copy" else "Оформление: пересылка")
        await show(callback, settings_text(store), settings_keyboard(store))

    @router.callback_query(F.data == "settings:notify")
    async def cb_settings_notify(callback: CallbackQuery) -> None:
        new_value = not store.notify_errors
        store.set_notify_errors(new_value)
        log.info("Уведомления об ошибках: %s", "включены" if new_value else "выключены")
        await callback.answer("Уведомления включены" if new_value else "Уведомления выключены")
        await show(callback, settings_text(store), settings_keyboard(store))

    @router.callback_query(F.data == "settings:delay")
    async def cb_settings_delay(callback: CallbackQuery) -> None:
        try:
            index = DELAY_CHOICES.index(store.delay)
        except ValueError:
            index = 0
        new_delay = DELAY_CHOICES[(index + 1) % len(DELAY_CHOICES)]
        store.set_delay(new_delay)
        log.info("Задержка изменена: %s сек", new_delay)
        await callback.answer(f"Задержка: {new_delay} сек")
        await show(callback, settings_text(store), settings_keyboard(store))

    @router.callback_query(F.data == "system:start")
    async def cb_start(callback: CallbackQuery) -> None:
        if store.enabled:
            await callback.answer("Уже запущено")
        else:
            store.set_enabled(True)
            log.info("Пересылка запущена")
            await callback.answer("▶️ Запущено")
        await show(callback, panel_text(store), main_menu())

    @router.callback_query(F.data == "system:pause")
    async def cb_pause(callback: CallbackQuery) -> None:
        if not store.enabled:
            await callback.answer("Уже остановлено")
        else:
            store.set_enabled(False)
            log.info("Пересылка остановлена")
            await callback.answer("⏸ Остановлено")
        await show(callback, panel_text(store), main_menu())

    @router.callback_query(F.data.startswith("src:open:"))
    async def cb_source_open(callback: CallbackQuery) -> None:
        source = callback.data.split(":", 2)[2]
        if not any(str(route.get("source")) == source for route in store.routes()):
            await callback.answer("Источник уже удалён", show_alert=True)
            await show(callback, sources_text(store), sources_keyboard(store))
            return
        await callback.answer()
        await show(callback, source_text(store, source), source_keyboard(store, source))

    @router.callback_query(F.data.startswith("src:toggle:"))
    async def cb_source_toggle(callback: CallbackQuery) -> None:
        source = callback.data.split(":", 2)[2]
        routes = [route for route in store.routes() if str(route.get("source")) == source]
        if not routes:
            await callback.answer("Источник уже удалён", show_alert=True)
            await show(callback, sources_text(store), sources_keyboard(store))
            return
        all_enabled = all(route.get("enabled", True) for route in routes)
        changed = store.set_source_enabled(source, not all_enabled)
        log.info("Источник %s: %s (%s маршрутов)", source, "выключен" if all_enabled else "включён", changed)
        await callback.answer("⏸ Выключено" if all_enabled else "▶️ Включено")
        await show(callback, source_text(store, source), source_keyboard(store, source))

    @router.callback_query(F.data.startswith("src:del:"))
    async def cb_source_delete_ask(callback: CallbackQuery) -> None:
        source = callback.data.split(":", 2)[2]
        routes = [route for route in store.routes() if str(route.get("source")) == source]
        if not routes:
            await callback.answer("Источник уже удалён", show_alert=True)
            await show(callback, sources_text(store), sources_keyboard(store))
            return
        await callback.answer()
        await show(
            callback,
            f"🗑 Удалить источник <code>{html.escape(source)}</code>?\n\n"
            f"Будут удалены все его маршруты: <b>{len(routes)}</b>.",
            confirm_source_delete_keyboard(source),
        )

    @router.callback_query(F.data.startswith("src:delconfirm:"))
    async def cb_source_delete_confirm(callback: CallbackQuery) -> None:
        source = callback.data.split(":", 2)[2]
        removed = store.delete_source(source)
        log.info("Источник %s удалён (%s маршрутов)", source, removed)
        await callback.answer("Источник удалён" if removed else "Источник не найден", show_alert=not removed)
        await show(callback, sources_text(store), sources_keyboard(store))

    @router.callback_query(F.data.startswith("route:open:"))
    async def cb_route_open(callback: CallbackQuery) -> None:
        route_id = callback.data.split(":", 2)[2]
        route = store.route(route_id)
        if route is None:
            await callback.answer("Маршрут уже удалён", show_alert=True)
            await show(callback, routes_text(store), routes_keyboard(store))
            return
        await callback.answer()
        await show(callback, route_text(route), route_keyboard(route))

    @router.callback_query(F.data.startswith("route:toggle:"))
    async def cb_route_toggle(callback: CallbackQuery) -> None:
        route_id = callback.data.split(":", 2)[2]
        route = store.route(route_id)
        if route is None:
            await callback.answer("Маршрут уже удалён", show_alert=True)
            await show(callback, routes_text(store), routes_keyboard(store))
            return
        new_value = not route.get("enabled", True)
        store.update_route(route_id, enabled=new_value)
        log.info("Маршрут %s: %s", route_id, "включён" if new_value else "выключен")
        await callback.answer("▶️ Включён" if new_value else "⏸ Выключен")
        await show(callback, routes_text(store), routes_keyboard(store))

    @router.callback_query(F.data.startswith("route:vtoggle:"))
    async def cb_route_vtoggle(callback: CallbackQuery) -> None:
        route_id = callback.data.split(":", 2)[2]
        route = store.route(route_id)
        if route is None:
            await callback.answer("Маршрут уже удалён", show_alert=True)
            await show(callback, routes_text(store), routes_keyboard(store))
            return
        new_value = not route.get("enabled", True)
        updated = store.update_route(route_id, enabled=new_value)
        log.info("Маршрут %s: %s", route_id, "включён" if new_value else "выключен")
        await callback.answer("▶️ Включён" if new_value else "⏸ Выключен")
        if updated is not None:
            await show(callback, route_text(updated), route_keyboard(updated))

    @router.callback_query(F.data.startswith("route:del:"))
    async def cb_route_delete_ask(callback: CallbackQuery) -> None:
        route_id = callback.data.split(":", 2)[2]
        route = store.route(route_id)
        if route is None:
            await callback.answer("Маршрут уже удалён", show_alert=True)
            await show(callback, routes_text(store), routes_keyboard(store))
            return
        await callback.answer()
        await show(
            callback,
            "🗑 Удалить этот маршрут?\n\n"
            f"<code>{html.escape(str(route['source']))}</code>\n↓\n"
            f"<code>{html.escape(str(route['destination']))}</code>",
            confirm_route_delete_keyboard(route_id),
        )

    @router.callback_query(F.data.startswith("route:delconfirm:"))
    async def cb_route_delete_confirm(callback: CallbackQuery) -> None:
        route_id = callback.data.split(":", 2)[2]
        deleted = store.delete_route(route_id)
        log.info("Маршрут %s удалён: %s", route_id, deleted)
        await callback.answer("✅ Маршрут удалён" if deleted else "Маршрут не найден", show_alert=not deleted)
        await show(callback, routes_text(store), routes_keyboard(store))

    @router.callback_query(F.data == "route:add")
    async def cb_route_add(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        await state.set_state(AddRoute.source)
        if callback.message is not None:
            await callback.message.answer(
                "Шаг 1 из 2.\n\n"
                "Напиши канал, ОТКУДА брать сообщения.\n\n"
                "Например: @toporlive\n"
                "или ID канала.",
                reply_markup=cancel_button(),
            )

    @router.message(AddRoute.source)
    async def add_source(message: Message, state: FSMContext) -> None:
        try:
            title, normalized, via = await check_source(message.text or "")
        except ValueError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=cancel_button())
            return
        await state.update_data(add_source=normalized, add_source_title=title, add_source_via=via)
        await state.set_state(AddRoute.destination)
        await message.answer(
            f"ОТКУДА: <code>{html.escape(normalized)}</code>\n\n"
            "Теперь напиши канал, КУДА отправлять сообщения.\n\n"
            "Например: @smesh_news",
            reply_markup=cancel_button(),
        )

    @router.message(AddRoute.destination)
    async def add_destination(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        source = data.get("add_source")
        if not source:
            await state.clear()
            await message.answer("❌ Не понял, начни заново: /start", reply_markup=main_menu())
            return
        try:
            chat, normalized = await validate_target(bot, bot_id, message.text or "", "destination")
        except ValueError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=cancel_button())
            return
        if _same_target(source, normalized):
            await message.answer("❌ Это один и тот же канал. Напиши другой.", reply_markup=cancel_button())
            return
        if store.duplicate_exists(source, normalized):
            await message.answer("❌ Такая пересылка уже есть.", reply_markup=cancel_button())
            return
        await state.update_data(add_destination=normalized, add_destination_title=chat.title)
        await state.set_state(AddRoute.confirm)
        await message.answer(
            "Проверь, всё верно?\n\n"
            f"ОТКУДА:\n<code>{html.escape(source)}</code>\n\n"
            f"КУДА:\n<code>{html.escape(normalized)}</code>",
            reply_markup=add_confirm_keyboard(),
        )

    @router.callback_query(F.data == "route:create")
    async def cb_route_create(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        source = data.get("add_source")
        destination = data.get("add_destination")
        if not source or not destination:
            await callback.answer("Не понял, начни заново", show_alert=True)
            await state.clear()
            await show(callback, panel_text(store), main_menu())
            return
        if store.duplicate_exists(source, destination):
            await state.clear()
            await callback.answer("Такая пересылка уже есть", show_alert=True)
            await show(callback, panel_text(store), main_menu())
            return
        route = store.add_route(source, destination, via=str(data.get("add_source_via", "bot")))
        await state.clear()
        log.info("Маршрут создан: %s -> %s", source, destination)
        await callback.answer("✅ Готово")
        if callback.message is not None:
            await callback.message.answer(
                "✅ Готово! Пересылка включена.\n\n"
                f"Из <code>{html.escape(source)}</code>\n"
                f"в <code>{html.escape(destination)}</code>\n\n"
                "Теперь всё новое из первого канала будет появляться во втором.",
                reply_markup=main_menu(),
            )
        log.info("Маршрут включён: %s -> %s", route["source"], route["destination"])

    @router.callback_query(F.data.startswith("route:setsrc:"))
    async def cb_edit_source(callback: CallbackQuery, state: FSMContext) -> None:
        route_id = callback.data.split(":", 2)[2]
        route = store.route(route_id)
        if route is None:
            await callback.answer("Маршрут уже удалён", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(EditRoute.source)
        await state.update_data(edit_route_id=route_id)
        if callback.message is not None:
            await callback.message.answer(
                "Напиши новый канал, ОТКУДА брать сообщения.",
                reply_markup=cancel_button(),
            )

    @router.message(EditRoute.source)
    async def edit_source(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        route_id = data.get("edit_route_id")
        route = store.route(route_id) if route_id else None
        if route is None:
            await state.clear()
            await message.answer("❌ Не нашёл маршрут.", reply_markup=main_menu())
            return
        try:
            _title, normalized, via = await check_source(message.text or "")
        except ValueError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=cancel_button())
            return
        if store.duplicate_exists(normalized, route["destination"], ignore_id=route_id):
            await message.answer("❌ Такая пересылка уже есть.", reply_markup=cancel_button())
            return
        updated = store.update_route(route_id, source=normalized, via=via)
        await state.clear()
        log.info("Маршрут %s: источник изменён на %s", route_id, normalized)
        await message.answer(f"✅ Теперь берём из <code>{html.escape(normalized)}</code>.", reply_markup=main_menu())
        if updated is not None:
            await message.answer(route_text(updated), reply_markup=route_keyboard(updated))

    @router.callback_query(F.data.startswith("route:setdst:"))
    async def cb_edit_destination(callback: CallbackQuery, state: FSMContext) -> None:
        route_id = callback.data.split(":", 2)[2]
        route = store.route(route_id)
        if route is None:
            await callback.answer("Маршрут уже удалён", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(EditRoute.destination)
        await state.update_data(edit_route_id=route_id)
        if callback.message is not None:
            await callback.message.answer(
                "Напиши новый канал, КУДА отправлять сообщения.",
                reply_markup=cancel_button(),
            )

    @router.message(EditRoute.destination)
    async def edit_destination(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        route_id = data.get("edit_route_id")
        route = store.route(route_id) if route_id else None
        if route is None:
            await state.clear()
            await message.answer("❌ Не нашёл маршрут.", reply_markup=main_menu())
            return
        try:
            _chat, normalized = await validate_target(bot, bot_id, message.text or "", "destination")
        except ValueError as exc:
            await message.answer(html.escape(str(exc)), reply_markup=cancel_button())
            return
        if _same_target(route["source"], normalized):
            await message.answer("❌ Это один и тот же канал. Напиши другой.", reply_markup=cancel_button())
            return
        if store.duplicate_exists(route["source"], normalized, ignore_id=route_id):
            await message.answer("❌ Такая пересылка уже есть.", reply_markup=cancel_button())
            return
        updated = store.update_route(route_id, destination=normalized)
        await state.clear()
        log.info("Маршрут %s: назначение изменено на %s", route_id, normalized)
        await message.answer(
            f"✅ Теперь отправляем в <code>{html.escape(normalized)}</code>.",
            reply_markup=main_menu(),
        )
        if updated is not None:
            await message.answer(route_text(updated), reply_markup=route_keyboard(updated))

    @router.callback_query(F.data == "fsm:cancel")
    async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        await show(callback, panel_text(store), main_menu())

    @router.callback_query()
    async def cb_unknown(callback: CallbackQuery) -> None:
        await callback.answer("Действие недоступно")

    @router.message()
    async def msg_unknown(message: Message) -> None:
        await message.answer("Используйте /start для открытия панели.", reply_markup=main_menu())

    return router


def build_public_router() -> Router:
    router = Router(name="public")
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart())
    async def deny_start(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else "unknown"
        log.warning("Отказано в доступе: user_id=%s", user_id)
        await message.answer("❌ У вас нет доступа к управлению этим ботом.")

    @router.message()
    async def deny_message(message: Message) -> None:
        await message.answer("❌ У вас нет доступа к управлению этим ботом.")

    @router.callback_query()
    async def deny_callback(callback: CallbackQuery) -> None:
        await callback.answer("❌ У вас нет доступа к управлению этим ботом.", show_alert=True)

    return router


def build_channel_router(forwarder: Forwarder) -> Router:
    router = Router(name="channels")

    @router.channel_post()
    async def on_channel_post(message: Message) -> None:
        try:
            await forwarder.handle_incoming(message)
        except Exception:
            log.exception("Ошибка при обработке сообщения из канала")

    @router.message(F.chat.type.in_({"group", "supergroup"}))
    async def on_group_message(message: Message) -> None:
        try:
            await forwarder.handle_incoming(message)
        except Exception:
            log.exception("Ошибка при обработке сообщения из группы")

    return router


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        replaced = message
        for secret in self._secrets:
            replaced = replaced.replace(secret, "***")
        if replaced != message:
            record.msg = replaced
            record.args = None
        return True


def get_proxy_url() -> str:
    return (os.getenv("PROXY_URL") or "").strip()


def setup_logging(*secrets: str) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    handler.addFilter(SecretRedactionFilter(list(secrets)))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def run_bot() -> None:
    load_dotenv(ENV_PATH)
    token = (os.getenv("BOT_TOKEN") or "").strip()
    admin_raw = (os.getenv("ADMIN_ID") or "").strip()
    proxy_url = get_proxy_url()
    setup_logging(token, proxy_url)

    if not token or token in {"YOUR_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE"} or ":" not in token:
        log.error("BOT_TOKEN не задан. Заполните файл .env (см. .env.example).")
        raise SystemExit(1)
    try:
        admin_id = int(admin_raw)
    except ValueError:
        log.error("ADMIN_ID должен быть числом. Узнайте свой ID у @userinfobot.")
        raise SystemExit(1)
    if admin_id <= 0:
        log.error("ADMIN_ID указан неверно.")
        raise SystemExit(1)

    try:
        validate_token(token)
    except TokenValidationError as exc:
        log.error("BOT_TOKEN имеет неверный формат. Скопируйте токен из @BotFather целиком.")
        log.error("Техническая ошибка: %s", exc)
        raise SystemExit(1) from exc

    store = ConfigStore(CONFIG_PATH)
    stats = Stats()
    recent = RecentTracker()

    try:
        session = AiohttpSession(proxy=proxy_url) if proxy_url else None
        bot = Bot(token=token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except (ValueError, RuntimeError) as exc:
        if proxy_url:
            log.error("Не удалось настроить прокси: %s", exc)
            log.error("Установите библиотеку: pip install aiohttp-socks")
        else:
            log.error("Не удалось настроить подключение: %s", exc)
        raise SystemExit(1) from exc

    if proxy_url:
        log.info("Используется прокси для подключения к Telegram")

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил BOT_TOKEN. Проверьте токен в .env.")
        await bot.session.close()
        raise SystemExit(1)
    except TelegramAPIError as exc:
        if proxy_url:
            log.error("Не удалось подключиться через прокси. Проверьте PROXY_URL в .env.")
        else:
            log.error("Не удалось подключиться к Telegram.")
            log.error("Проверьте интернет, VPN или укажите прокси в .env (PROXY_URL).")
        log.error("Техническая ошибка: %s", exc)
        await bot.session.close()
        raise SystemExit(1)
    except Exception as exc:
        if proxy_url:
            log.error("Не удалось подключиться через прокси. Проверьте PROXY_URL в .env.")
        else:
            log.error("Не удалось подключиться к Telegram.")
            log.error("Проверьте интернет, VPN или укажите прокси в .env (PROXY_URL).")
        log.error("Техническая ошибка: %s", exc)
        await bot.session.close()
        raise SystemExit(1) from exc

    dispatcher = Dispatcher(storage=MemoryStorage())
    forwarder = Forwarder(bot=bot, store=store, stats=stats, recent=recent, admin_id=admin_id)

    userbot = None
    api_id = (os.getenv("API_ID") or "").strip()
    api_hash = (os.getenv("API_HASH") or "").strip()
    phone = (os.getenv("PHONE") or "").strip()
    if api_id and api_hash:
        if UserbotSource is None:
            log.error("Для режима «от твоего аккаунта» установите библиотеку: pip install -r requirements.txt")
        else:
            userbot = UserbotSource(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                session_path=os.path.join(BASE_DIR, "userbot"),
                normalizer=normalize_channel_input,
                forwarder=forwarder,
                proxy=parse_proxy_url(proxy_url) if parse_proxy_url is not None else None,
            )
            try:
                await userbot.start()
                log.info("Источники: читаю твоим аккаунтом")
            except Exception as exc:  # noqa: BLE001
                log.error("Не удалось войти в аккаунт: %s", exc)
                log.error("Проверь API_ID, API_HASH и PHONE в .env. Бот продолжит работать без этого режима.")
                userbot = None
    if userbot is None:
        log.info("Источники: бот-админ в канале или публичный канал через веб-страницу")

    dispatcher.include_router(build_admin_router(bot, store, stats, admin_id, me.id, userbot, proxy_url))
    dispatcher.include_router(build_public_router())
    dispatcher.include_router(build_channel_router(forwarder))

    poller = PreviewPoller(store=store, forwarder=forwarder, proxy_url=proxy_url)
    await poller.start()

    log.info("Бот запущен: @%s", me.username)
    routes = store.routes()
    log.info("Загружено маршрутов: %s", len(routes))
    for route in routes:
        if route.get("enabled", True):
            log.info("Маршрут включён: %s -> %s", route["source"], route["destination"])
        else:
            log.info("Маршрут выключен: %s -> %s", route["source"], route["destination"])
    log.info("Общая пересылка: %s", "включена" if store.enabled else "остановлена")

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            allowed_updates=["message", "channel_post", "callback_query"],
        )
    finally:
        await poller.stop()
        if userbot is not None:
            await userbot.stop()
        await forwarder.close()
        await bot.session.close()
        log.info("Бот остановлен")


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
