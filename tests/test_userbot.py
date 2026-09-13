import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon.tl.types import Channel

import main
from userbot import UserbotSource, parse_proxy_url


class FakeMessage:
    def __init__(
        self,
        message_id=1,
        chat=None,
        text=None,
        media=None,
        file_size=None,
        kind="text",
    ):
        self.id = message_id
        self.chat_id = chat.id
        self._chat = chat
        self.message = text
        self.entities = None
        self.media = media
        self.file = SimpleNamespace(size=file_size) if file_size is not None else None
        self.grouped_id = None
        self.photo = kind == "photo"
        self.video = kind == "video"
        self.voice = False
        self.audio = False
        self.sticker = False
        self.gif = False

    async def get_chat(self):
        return self._chat

    async def download_media(self, file=None):
        path = Path(file) / f"media_{self.id}.bin"
        path.write_bytes(b"data")
        return str(path)


def make_chat(chat_id=-100111, username="toporlive", noforwards=False):
    return SimpleNamespace(id=chat_id, username=username, noforwards=noforwards, title="Test")


def make_forwarder(store):
    bot = SimpleNamespace(
        id=42,
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_video=AsyncMock(),
        send_document=AsyncMock(),
        send_audio=AsyncMock(),
        send_voice=AsyncMock(),
        send_sticker=AsyncMock(),
        send_animation=AsyncMock(),
        send_media_group=AsyncMock(),
    )
    stats = main.Stats()
    forwarder = main.Forwarder(
        bot=bot,
        store=store,
        stats=stats,
        recent=main.RecentTracker(),
        admin_id=999,
    )
    return forwarder, bot, stats


def make_userbot(forwarder):
    return UserbotSource(
        api_id="1",
        api_hash="hash",
        phone="",
        session_path="test-session",
        normalizer=main.normalize_channel_input,
        forwarder=forwarder,
    )


class TestUserbotDelivery(unittest.TestCase):
    def test_text_message_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            store.add_route("@toporlive", "@smesh_news", via="account")
            forwarder, bot, stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            chat = make_chat()
            message = FakeMessage(10, chat=chat, text="Привет")
            asyncio.run(userbot._process([message]))
            self.assertEqual(stats.received, 1)
            self.assertEqual(stats.forwarded, 1)
            bot.send_message.assert_awaited_once_with("@smesh_news", "Привет")
            asyncio.run(userbot._process([message]))
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.skipped, 1)

    def test_photo_is_sent_with_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            store.add_route("@toporlive", "@smesh_news", via="account")
            forwarder, bot, stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            chat = make_chat()
            message = FakeMessage(11, chat=chat, text="Подпись", media=object(), file_size=1000, kind="photo")
            asyncio.run(userbot._process([message]))
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(bot.send_photo.await_count, 1)
            args, kwargs = bot.send_photo.await_args
            self.assertEqual(args[0], "@smesh_news")
            self.assertEqual(kwargs.get("caption"), "Подпись")

    def test_album_is_sent_as_media_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            store.add_route("@toporlive", "@smesh_news", via="account")
            forwarder, bot, stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            chat = make_chat()
            messages = [
                FakeMessage(20, chat=chat, media=object(), file_size=1000, kind="photo"),
                FakeMessage(21, chat=chat, media=object(), file_size=1000, kind="photo"),
            ]
            asyncio.run(userbot._process(messages))
            self.assertEqual(stats.forwarded, 2)
            self.assertEqual(bot.send_media_group.await_count, 1)

    def test_large_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            store.add_route("@toporlive", "@smesh_news", via="account")
            forwarder, bot, stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            chat = make_chat()
            message = FakeMessage(
                30,
                chat=chat,
                media=object(),
                file_size=100 * 1024 * 1024,
                kind="video",
            )
            asyncio.run(userbot._process([message]))
            self.assertEqual(stats.forwarded, 0)
            self.assertEqual(stats.errors, 1)
            self.assertEqual(bot.send_video.await_count, 0)
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(bot.send_message.await_args.args[0], 999)

    def test_protected_channel_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            store.add_route("@toporlive", "@smesh_news", via="account")
            forwarder, bot, stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            chat = make_chat(noforwards=True)
            message = FakeMessage(40, chat=chat, text="Секрет")
            asyncio.run(userbot._process([message]))
            self.assertEqual(stats.forwarded, 0)
            self.assertEqual(stats.errors, 1)
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(bot.send_message.await_args.args[0], 999)
            self.assertIn("запрещ", bot.send_message.await_args.args[1])

    def test_unknown_source_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            store.add_route("@toporlive", "@smesh_news", via="account")
            forwarder, bot, stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            chat = make_chat(username="unknown")
            message = FakeMessage(50, chat=chat, text="Привет")
            asyncio.run(userbot._process([message]))
            self.assertEqual(stats.received, 0)
            self.assertEqual(bot.send_message.await_count, 0)


class TestUserbotValidation(unittest.TestCase):
    def test_validate_source_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            forwarder, _bot, _stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            entity = Channel(id=-100111, title="Test", photo=None, date=None, username="toporlive")
            userbot.client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))
            chat, normalized = asyncio.run(userbot.validate_source("@toporlive"))
            self.assertEqual(normalized, "@toporlive")
            self.assertEqual(chat.title, "Test")

    def test_validate_source_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = main.ConfigStore(str(Path(tmp) / "config.json"))
            forwarder, _bot, _stats = make_forwarder(store)
            userbot = make_userbot(forwarder)
            userbot.client = SimpleNamespace(get_entity=AsyncMock(side_effect=ValueError("no entity")))
            with self.assertRaises(ValueError) as context:
                asyncio.run(userbot.validate_source("@missing"))
            self.assertIn("Не нашёл", str(context.exception))

    def test_parse_proxy_url(self):
        proxy = parse_proxy_url("http://user:pass@127.0.0.1:8080")
        self.assertEqual(proxy["proxy_type"], "http")
        self.assertEqual(proxy["addr"], "127.0.0.1")
        self.assertEqual(proxy["port"], 8080)
        self.assertEqual(proxy["username"], "user")
        self.assertEqual(proxy["password"], "pass")
        socks = parse_proxy_url("socks5://127.0.0.1:1080")
        self.assertEqual(socks["proxy_type"], "socks5")
        self.assertNotIn("username", socks)
        self.assertIsNone(parse_proxy_url(""))
        self.assertIsNone(parse_proxy_url("ftp://127.0.0.1:21"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
