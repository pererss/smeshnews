from __future__ import annotations

import asyncio
import html as html_lib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
)

log = logging.getLogger("smesh.preview")

POLL_INTERVAL = 20.0
MAX_POSTS_PER_POLL = 10
MAX_CAPTION = 1024
MAX_MESSAGE = 4096
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MESSAGE_START_RE = re.compile(r'<div class="tgme_widget_message ')
POST_ID_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
PHOTO_WRAP_RE = re.compile(r'<a[^>]*class="[^"]*tgme_widget_message_photo_wrap[^"]*"[^>]*>', re.DOTALL)
BG_IMAGE_RE = re.compile(r"background-image:url\('([^']+)'\)")
VIDEO_RE = re.compile(r'<video[^>]+src="([^"]+)"')
AUDIO_RE = re.compile(r'<audio[^>]+src="([^"]+)"')
DOC_WRAP_RE = re.compile(
    r'<a[^>]*class="[^"]*tgme_widget_message_document_wrap[^"]*"[^>]*>(.*?)</a>',
    re.DOTALL,
)
DOC_TITLE_RE = re.compile(r'<div class="tgme_widget_message_document_title[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
DOC_EXTRA_RE = re.compile(r'<div class="tgme_widget_message_document_extra[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
ANCHOR_RE = re.compile(r"<a\s+[^>]*>")
TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
EMOJI_RE = re.compile(r'<i[^>]*class="emoji"[^>]*>.*?</i>', re.DOTALL)
SPAN_RE = re.compile(r"</?span[^>]*>")
TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.DOTALL)
NAMED_ENTITY_RE = re.compile(r"&([a-zA-Z]+);")
SAFE_ENTITIES = {"lt", "gt", "amp", "quot"}


@dataclass
class PreviewPost:
    message_id: int
    text: str = ""
    photos: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    audios: list[str] = field(default_factory=list)
    documents: list[tuple[str, str]] = field(default_factory=list)


def _fix_anchor(match: re.Match) -> str:
    href = re.search(r'href="([^"]*)"', match.group(0))
    if not href:
        return ""
    return f'<a href="{href.group(1)}">'


def _fix_entities(text: str) -> str:
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name in SAFE_ENTITIES:
            return match.group(0)
        return html_lib.unescape(match.group(0)).replace("\xa0", " ")

    return NAMED_ENTITY_RE.sub(replace, text)


def clean_text(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = EMOJI_RE.sub("", text)
    text = TG_EMOJI_RE.sub(r"\1", text)
    text = SPAN_RE.sub("", text)
    text = ANCHOR_RE.sub(_fix_anchor, text)
    text = _fix_entities(text)
    return text.strip()


def parse_posts(page: str) -> list[PreviewPost]:
    starts = [match.start() for match in MESSAGE_START_RE.finditer(page)]
    if not starts:
        return []
    starts.append(len(page))
    posts: list[PreviewPost] = []
    for index in range(len(starts) - 1):
        block = page[starts[index]:starts[index + 1]]
        id_match = POST_ID_RE.search(block)
        if not id_match:
            continue
        post = PreviewPost(message_id=int(id_match.group(2)))
        text_match = TEXT_RE.search(block)
        if text_match:
            post.text = clean_text(text_match.group(1))
        for wrap in PHOTO_WRAP_RE.findall(block):
            url = BG_IMAGE_RE.search(wrap)
            if url:
                post.photos.append(url.group(1))
        post.videos.extend(VIDEO_RE.findall(block))
        post.audios.extend(AUDIO_RE.findall(block))
        for document in DOC_WRAP_RE.findall(block):
            title_match = DOC_TITLE_RE.search(document)
            extra_match = DOC_EXTRA_RE.search(document)
            title = clean_text(title_match.group(1)) if title_match else "документ"
            size = clean_text(extra_match.group(1)) if extra_match else ""
            post.documents.append((title, size))
        if post.text or post.photos or post.videos or post.audios or post.documents:
            posts.append(post)
    return posts


def parse_title(page: str) -> str:
    match = TITLE_RE.search(page)
    if match:
        return html_lib.unescape(match.group(1))
    return ""


async def fetch_page(username: str, proxy_url: str | None = None) -> str | None:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    headers = {"User-Agent": USER_AGENT}
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
            session.get(f"https://t.me/s/{username}", proxy=proxy_url) as response,
        ):
            if response.status != 200:
                return None
            return await response.text()
    except aiohttp.ClientError as exc:
        log.warning("Веб-режим: не удалось открыть %s: %s", username, exc)
        return None


def remove_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def download_to_temp(url: str, proxy_url: str | None = None) -> str | None:
    timeout = aiohttp.ClientTimeout(total=120)
    headers = {"User-Agent": USER_AGENT}
    suffix = os.path.splitext(urlparse(url).path)[1][:10] or ".bin"
    fd, path = tempfile.mkstemp(prefix="smesh-", suffix=suffix)
    os.close(fd)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
            session.get(url, proxy=proxy_url) as response,
        ):
            if response.status != 200:
                raise RuntimeError(f"download_failed: HTTP {response.status}")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ValueError("file_too_big")
        await asyncio.to_thread(_write_bytes, path, bytes(data))
        return path
    except BaseException:
        remove_file(path)
        raise


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as file:
        file.write(data)


async def check_source(raw: str, normalizer: Any, proxy_url: str | None = None) -> tuple[str, str]:
    normalized = normalizer(raw)
    if not normalized.startswith("@"):
        raise ValueError(
            "❌ Для веб-режима нужен публичный канал (@username).\n"
            "Приватные каналы и группы так читать нельзя."
        )
    username = normalized[1:]
    page = await fetch_page(username, proxy_url)
    if page is None:
        raise ValueError(
            "❌ Не нашёл веб-версию этого канала.\n\n"
            "Проверь:\n"
            "• правильно ли написан @username;\n"
            "• это публичный канал (у приватных веб-версии нет)."
        )
    title = parse_title(page) or normalized
    if "tgme_widget_message" not in page and "tgme_channel_info" not in page:
        raise ValueError("❌ Эта страница не похожа на канал.")
    return title, normalized


class PreviewPoller:
    def __init__(
        self,
        *,
        store: Any,
        forwarder: Any,
        proxy_url: str = "",
        interval: float = POLL_INTERVAL,
    ) -> None:
        self.store = store
        self.forwarder = forwarder
        self.proxy_url = proxy_url or None
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stopped = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("Ошибка веб-режима: %s", exc)
            await asyncio.sleep(self.interval)

    async def poll_once(self) -> None:
        if not self.store.enabled:
            return
        routes = [
            route
            for route in self.store.routes()
            if route.get("via") == "preview" and route.get("enabled", True)
        ]
        for route in routes:
            await self._poll_route(route)

    async def _poll_route(self, route: dict[str, Any]) -> None:
        source = str(route["source"])
        if not source.startswith("@"):
            return
        username = source[1:]
        page = await self._fetch(username)
        if page is None:
            return
        posts = parse_posts(page)
        if not posts:
            return
        posts.sort(key=lambda post: post.message_id)
        last_id = int(route.get("last_processed_message_id", 0) or 0)
        if last_id == 0:
            self.store.set_last_processed(str(route["id"]), posts[-1].message_id)
            log.info("Веб-режим: %s подключён, жду новые сообщения", source)
            return
        new_posts = [post for post in posts if post.message_id > last_id]
        if not new_posts:
            return
        for post in new_posts[:MAX_POSTS_PER_POLL]:
            if not self.forwarder.mark_processed(route, post.message_id):
                continue
            try:
                await self._send(route, post)
                self.forwarder.stats.add_received()
                self.forwarder.stats.add_forwarded()
                log.info(
                    "Сообщение успешно отправлено (веб-режим): %s -> %s",
                    route["source"],
                    route["destination"],
                )
            except Exception as exc:  # noqa: BLE001
                label = f'<a href="https://t.me/{username}/{post.message_id}">открыть сообщение</a>'
                await self.forwarder.report_error(route, label, exc)
        if len(new_posts) > MAX_POSTS_PER_POLL:
            log.warning(
                "Веб-режим: в %s больше %s новых сообщений, лишние пропущены",
                source,
                MAX_POSTS_PER_POLL,
            )

    async def _fetch(self, username: str) -> str | None:
        return await fetch_page(username, self.proxy_url)

    async def _send(self, route: dict[str, Any], post: PreviewPost) -> None:
        bot = self.forwarder.bot
        destination = self.forwarder.destination_of(route)
        media: list[tuple[str, str]] = [("photo", url) for url in post.photos]
        media += [("video", url) for url in post.videos]
        media += [("audio", url) for url in post.audios]
        if not media:
            body = post.text or self._documents_text(post)
            if body:
                await bot.send_message(destination, body[:MAX_MESSAGE])
            return
        caption = post.text[:MAX_CAPTION] if post.text else None
        if len(media) == 1:
            sent = await self._send_single(destination, media[0], caption)
            if not sent and caption:
                await bot.send_message(destination, caption)
        else:
            await self._send_album(destination, media, caption)
        if post.documents:
            documents = self._documents_text(post)
            if documents:
                await bot.send_message(destination, documents[:MAX_MESSAGE])

    async def _send_single(self, destination: Any, item: tuple[str, str], caption: str | None) -> bool:
        bot = self.forwarder.bot
        kind, url = item
        try:
            await self._send_by_url(destination, kind, url, caption)
            return True
        except TelegramAPIError as exc:
            log.warning("Ссылку отправить не удалось (%s), скачиваю файл", exc)
        try:
            path = await download_to_temp(url, self.proxy_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать файл (%s), пропускаю вложение", exc)
            return False
        if path is None:
            return False
        try:
            file = FSInputFile(path)
            if kind == "photo":
                await bot.send_photo(destination, file, caption=caption)
            elif kind == "video":
                await bot.send_video(destination, file, caption=caption)
            else:
                await bot.send_audio(destination, file, caption=caption)
            return True
        finally:
            remove_file(path)

    async def _send_by_url(self, destination: Any, kind: str, url: str, caption: str | None) -> None:
        bot = self.forwarder.bot
        if kind == "photo":
            await bot.send_photo(destination, url, caption=caption)
        elif kind == "video":
            await bot.send_video(destination, url, caption=caption)
        else:
            await bot.send_audio(destination, url, caption=caption)

    def _build_album(
        self,
        chunk: list[tuple[str, str]],
        caption: str | None,
        paths: list[str],
    ) -> list[Any]:
        items: list[Any] = []
        for index, (kind, url) in enumerate(chunk):
            item_caption = caption if index == 0 else None
            source: Any = FSInputFile(paths[index]) if paths else url
            if kind == "photo":
                items.append(InputMediaPhoto(media=source, caption=item_caption))
            else:
                items.append(InputMediaVideo(media=source, caption=item_caption))
        return items

    async def _send_album(
        self,
        destination: Any,
        media: list[tuple[str, str]],
        caption: str | None,
    ) -> None:
        bot = self.forwarder.bot
        if any(kind == "audio" for kind, _ in media):
            for index, (kind, url) in enumerate(media):
                await self._send_single(destination, (kind, url), caption if index == 0 else None)
            return
        for start in range(0, len(media), 10):
            chunk = media[start:start + 10]
            chunk_caption = caption if start == 0 else None
            if len(chunk) == 1:
                sent = await self._send_single(destination, chunk[0], chunk_caption)
                if not sent and chunk_caption:
                    await bot.send_message(destination, chunk_caption)
                continue
            if await self._try_send_album(destination, chunk, chunk_caption):
                continue
            await self._send_without_video(destination, chunk, chunk_caption)

    async def _try_send_album(
        self,
        destination: Any,
        chunk: list[tuple[str, str]],
        caption: str | None,
    ) -> bool:
        bot = self.forwarder.bot
        try:
            await bot.send_media_group(destination, media=self._build_album(chunk, caption, []))
            return True
        except TelegramAPIError as exc:
            log.warning("Альбом по ссылкам не отправился (%s), скачиваю файлы", exc)
        paths: list[str] = []
        try:
            for _kind, url in chunk:
                paths.append(await download_to_temp(url, self.proxy_url))
            await bot.send_media_group(destination, media=self._build_album(chunk, caption, paths))
            return True
        except TelegramAPIError as exc:
            log.warning("Альбом файлами не отправился (%s)", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось скачать альбом (%s)", exc)
            return False
        finally:
            for path in paths:
                if path:
                    remove_file(path)

    async def _send_without_video(
        self,
        destination: Any,
        chunk: list[tuple[str, str]],
        caption: str | None,
    ) -> None:
        photos = [item for item in chunk if item[0] == "photo"]
        if len(photos) > 1 and await self._try_send_album(destination, photos, caption):
            return
        if photos:
            for index, item in enumerate(photos):
                sent = await self._send_single(destination, item, caption if index == 0 else None)
                if not sent and index == 0 and caption:
                    await self.forwarder.bot.send_message(destination, caption)
            return
        if caption:
            await self.forwarder.bot.send_message(destination, caption)

    @staticmethod
    def _documents_text(post: PreviewPost) -> str:
        if not post.documents:
            return ""
        lines = []
        for title, size in post.documents:
            lines.append(f"📎 {title}" + (f" ({size})" if size else ""))
        return "\n".join(lines)
