from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from aiogram.types import (
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telethon import TelegramClient, events
from telethon.extensions import html as tl_html
from telethon.tl.types import Channel, Chat

log = logging.getLogger("smesh.userbot")

MAX_UPLOAD_BYTES = 49 * 1024 * 1024
MAX_MEDIA_GROUP = 10
PROXY_TYPES = {"socks5": "socks5", "socks4": "socks4", "http": "http", "https": "http"}


def parse_proxy_url(url: str) -> dict[str, Any] | None:
    if not url:
        return None
    parsed = urlparse(url)
    proxy_type = PROXY_TYPES.get((parsed.scheme or "http").lower())
    if proxy_type is None or not parsed.hostname or not parsed.port:
        return None
    proxy: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": parsed.hostname,
        "port": parsed.port,
        "rdns": True,
    }
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


class UserbotSource:
    def __init__(
        self,
        *,
        api_id: str,
        api_hash: str,
        phone: str,
        session_path: str,
        normalizer: Callable[[str], str],
        forwarder: Any,
        proxy: dict[str, Any] | None = None,
    ) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.phone = phone
        self.session_path = session_path
        self.normalize = normalizer
        self.forwarder = forwarder
        self.proxy = proxy
        self.client: TelegramClient | None = None

    async def start(self) -> None:
        self.client = TelegramClient(self.session_path, self.api_id, self.api_hash, proxy=self.proxy)
        await self.client.start(phone=self.phone or None)
        me = await self.client.get_me()
        name = getattr(me, "username", None) or getattr(me, "first_name", "?")
        log.info("Аккаунт подключён: %s", name)
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_album, events.Album())

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()

    async def validate_source(self, raw: str) -> tuple[Any, str]:
        normalized = self.normalize(raw)
        target: Any = int(normalized) if normalized.lstrip("-").isdigit() else normalized
        try:
            entity = await self.client.get_entity(target)
        except Exception as exc:
            raise ValueError(
                "❌ Не нашёл этот канал.\n\n"
                "Проверь:\n"
                "• правильно ли написан @username;\n"
                "• подписан ли твой аккаунт на этот канал."
            ) from exc
        if not isinstance(entity, (Channel, Chat)):
            raise ValueError("❌ Это не канал и не группа.")  # noqa: TRY004
        return entity, normalized

    async def _on_message(self, event: Any) -> None:
        message = event.message
        if message is None or message.grouped_id:
            return
        await self._process([message])

    async def _on_album(self, event: Any) -> None:
        messages = list(event.messages or [])
        if messages:
            await self._process(messages)

    async def _process(self, messages: list[Any]) -> None:
        forwarder = self.forwarder
        if not forwarder.store.enabled:
            return
        first = messages[0]
        try:
            chat = await first.get_chat()
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось определить канал: %s", exc)
            return
        chat_id = first.chat_id
        username = getattr(chat, "username", None)
        routes = forwarder.store.routes_for_source(chat_id, username)
        routes = [
            route
            for route in routes
            if route.get("enabled", True) and route.get("via", "bot") == "account"
        ]
        if not routes:
            return
        if getattr(chat, "noforwards", False):
            log.warning("Канал %s запрещает сохранение контента — пропускаю", username or chat_id)
            for route in routes:
                if forwarder.mark_processed(route, first.id):
                    await forwarder.report_error(route, self._label(first, username), RuntimeError("noforwards"))
            return
        forwarder.stats.add_received()
        log.info("Получено новое сообщение (аккаунт): chat=%s id=%s", chat_id, first.id)
        for route in routes:
            if not forwarder.mark_processed(route, first.id):
                continue
            try:
                await self._deliver(route, messages)
            except Exception as exc:  # noqa: BLE001
                await forwarder.report_error(route, self._label(first, username), exc)

    async def _deliver(self, route: dict[str, Any], messages: list[Any]) -> None:
        forwarder = self.forwarder
        if forwarder.store.delay:
            await asyncio.sleep(forwarder.store.delay)
        for message in messages:
            file_info = getattr(message, "file", None)
            size = getattr(file_info, "size", None)
            if size and size > MAX_UPLOAD_BYTES:
                raise ValueError("file_too_big")
        destination = forwarder.destination_of(route)
        bot = forwarder.bot
        with tempfile.TemporaryDirectory() as tmp:
            paths: list[str | None] = []
            for message in messages:
                path = None
                if getattr(message, "media", None) is not None:
                    path = await message.download_media(file=tmp)
                paths.append(path)
            if len(messages) == 1:
                await self._send_single(bot, destination, messages[0], paths[0])
            else:
                await self._send_album(bot, destination, messages, paths)
        forwarder.stats.add_forwarded(len(messages))
        log.info("Сообщение успешно отправлено (аккаунт): %s -> %s", route["source"], route["destination"])

    async def _send_single(self, bot: Any, destination: Any, message: Any, path: str | None) -> None:
        text = self._html(message)
        if path is None:
            if text:
                await bot.send_message(destination, text)
            return
        file = FSInputFile(path)
        kwargs = {"caption": text} if text else {}
        if getattr(message, "photo", None):
            await bot.send_photo(destination, file, **kwargs)
        elif getattr(message, "video", None):
            await bot.send_video(destination, file, **kwargs)
        elif getattr(message, "voice", None):
            await bot.send_voice(destination, file, **kwargs)
        elif getattr(message, "audio", None):
            await bot.send_audio(destination, file, **kwargs)
        elif getattr(message, "sticker", None):
            await bot.send_sticker(destination, file)
        elif getattr(message, "gif", None):
            await bot.send_animation(destination, file, **kwargs)
        else:
            await bot.send_document(destination, file, **kwargs)

    async def _send_album(
        self,
        bot: Any,
        destination: Any,
        messages: list[Any],
        paths: list[str | None],
    ) -> None:
        media: list[Any] = []
        for message, path in zip(messages, paths):
            if path is None:
                await self._send_individually(bot, destination, messages, paths)
                return
            caption = self._html(message) or None
            file = FSInputFile(path)
            if getattr(message, "photo", None):
                media.append(InputMediaPhoto(media=file, caption=caption))
            elif getattr(message, "video", None):
                media.append(InputMediaVideo(media=file, caption=caption))
            else:
                media.append(InputMediaDocument(media=file, caption=caption))
        for start in range(0, len(media), MAX_MEDIA_GROUP):
            chunk = media[start:start + MAX_MEDIA_GROUP]
            if len(chunk) == 1:
                await self._send_single(bot, destination, messages[start], paths[start])
            else:
                await bot.send_media_group(destination, media=chunk)

    async def _send_individually(
        self,
        bot: Any,
        destination: Any,
        messages: list[Any],
        paths: list[str | None],
    ) -> None:
        for message, path in zip(messages, paths):
            await self._send_single(bot, destination, message, path)

    @staticmethod
    def _html(message: Any) -> str:
        text = getattr(message, "message", None) or ""
        if not text:
            return ""
        try:
            return tl_html.unparse(text, getattr(message, "entities", None) or [])
        except Exception:  # noqa: BLE001
            return text

    @staticmethod
    def _label(message: Any, username: str | None) -> str:
        if username:
            return f'<a href="https://t.me/{username}/{message.id}">открыть сообщение</a>'
        return f"<code>id={message.id}</code>"
