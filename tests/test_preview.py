import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

import main
import preview
from preview import PreviewPoller, clean_text, parse_posts, parse_title

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tme_sample.html"


def make_page(posts):
    blocks = []
    for post_id, text, photos in posts:
        media = "".join(
            f"""<a class="tgme_widget_message_photo_wrap" href="https://t.me/chan/{post_id}" style="background-image:url('{url}')"></a>"""
            for url in photos
        )
        text_html = f'<div class="tgme_widget_message_text js-message_text">{text}</div>' if text else ""
        blocks.append(f'<div class="tgme_widget_message " data-post="chan/{post_id}">{media}{text_html}</div>')
    return "<html><body>" + "".join(blocks) + "</body></html>"


def make_album_page(post_id, text, photo_urls=(), video_urls=()):
    media = "".join(
        f"""<a class="tgme_widget_message_photo_wrap" href="https://t.me/chan/{post_id}" style="background-image:url('{url}')"></a>"""
        for url in photo_urls
    )
    media += "".join(
        f"""<div class="tgme_widget_message_video_wrap"><video src="{url}"></video></div>"""
        for url in video_urls
    )
    text_html = f'<div class="tgme_widget_message_text js-message_text">{text}</div>' if text else ""
    return f'<html><body><div class="tgme_widget_message " data-post="chan/{post_id}">{media}{text_html}</div></body></html>'


def make_store(tmp_path):
    return main.ConfigStore(str(Path(tmp_path) / "config.json"))


def make_forwarder(store):
    bot = SimpleNamespace(
        id=42,
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_video=AsyncMock(),
        send_audio=AsyncMock(),
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


class TestPreviewParser(unittest.TestCase):
    def test_parse_real_fixture(self):
        page = FIXTURE.read_text(encoding="utf-8")
        posts = parse_posts(page)
        self.assertEqual([post.message_id for post in posts], [441, 445, 452])
        video_post = posts[0]
        self.assertEqual(len(video_post.videos), 1)
        self.assertTrue(video_post.videos[0].startswith("https://cdn"))
        text_post = posts[1]
        self.assertIn("May Features", text_post.text)
        self.assertNotIn("onclick", text_post.text)
        photo_post = posts[2]
        self.assertEqual(len(photo_post.photos), 1)
        self.assertTrue(photo_post.photos[0].startswith("https://cdn"))
        self.assertIn("Ephemeral Bot Messages", photo_post.text)

    def test_clean_text(self):
        raw = '<b>Жирный</b><br/><br/><a href="https://x.test/?a=1&amp;b=2" onclick="x">ссылка</a>&nbsp;конец'
        cleaned = clean_text(raw)
        self.assertIn("<b>Жирный</b>", cleaned)
        self.assertIn("\n\n", cleaned)
        self.assertIn('<a href="https://x.test/?a=1&amp;b=2">ссылка</a>', cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertIn(" конец", cleaned)

    def test_clean_text_emoji_and_span(self):
        raw = '<span class="x"><i class="emoji" style="background:url(1)"></i><tg-emoji emoji-id="7">😀</tg-emoji>привет</span>'
        cleaned = clean_text(raw)
        self.assertNotIn("<i", cleaned)
        self.assertNotIn("<span", cleaned)
        self.assertIn("😀привет", cleaned)

    def test_parse_title(self):
        page = '<html><head><meta property="og:title" content="Test &amp; Co"></head></html>'
        self.assertEqual(parse_title(page), "Test & Co")


class TestPreviewPoller(unittest.IsolatedAsyncioTestCase):
    async def test_first_poll_marks_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            forwarder, bot, _stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(return_value=make_page([(5, "старое", [])]))
            await poller.poll_once()
            self.assertEqual(bot.send_message.await_count, 0)
            self.assertEqual(store.route(route["id"])["last_processed_message_id"], 5)

    async def test_new_post_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 5)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(return_value=make_page([(5, "старое", []), (6, "новое", [])]))
            await poller.poll_once()
            bot.send_message.assert_awaited_once_with("@dest", "новое")
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.received, 1)

    async def test_photo_post_is_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            page = make_page([(2, "подпись", ["https://cdn.test/p.jpg"])])
            poller._fetch = AsyncMock(return_value=page)
            await poller.poll_once()
            bot.send_photo.assert_awaited_once_with("@dest", "https://cdn.test/p.jpg", caption="подпись")
            self.assertEqual(stats.forwarded, 1)

    async def test_url_failure_downloads_and_uploads(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            page = make_page([(2, "подпись", ["https://cdn.test/p.jpg"])])
            poller._fetch = AsyncMock(return_value=page)
            error = TelegramBadRequest(
                method=None,
                message="Bad Request: failed to get HTTP URL content",
            )
            bot.send_photo.side_effect = [error, None]
            media_file = Path(tmp) / "media.jpg"
            media_file.write_bytes(b"data")
            with patch("preview.download_to_temp", new=AsyncMock(return_value=str(media_file))):
                await poller.poll_once()
            self.assertEqual(bot.send_photo.await_count, 2)
            self.assertIsInstance(bot.send_photo.await_args.args[1], FSInputFile)
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.errors, 0)

    async def test_album_url_failure_downloads_and_sends_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            page = make_page([(2, "подпись", ["https://cdn.test/1.jpg", "https://cdn.test/2.jpg"])])
            poller._fetch = AsyncMock(return_value=page)
            error = TelegramBadRequest(
                method=None,
                message="Bad Request: failed to get HTTP URL content",
            )
            bot.send_media_group.side_effect = [error, None]
            files = []
            for name in ("1.jpg", "2.jpg"):
                path = Path(tmp) / name
                path.write_bytes(b"data")
                files.append(str(path))
            with patch("preview.download_to_temp", new=AsyncMock(side_effect=files)):
                await poller.poll_once()
            self.assertEqual(bot.send_media_group.await_count, 2)
            media_items = bot.send_media_group.await_args.kwargs["media"]
            self.assertIsInstance(media_items[0].media, FSInputFile)
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.errors, 0)

    async def test_video_post_is_sent_with_caption(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(
                return_value=make_album_page(2, "подпись", [], ["https://cdn.test/v.mp4"])
            )
            await poller.poll_once()
            bot.send_video.assert_awaited_once_with("@dest", "https://cdn.test/v.mp4", caption="подпись")
            self.assertEqual(stats.forwarded, 1)

    async def test_video_download_failure_sends_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(
                return_value=make_album_page(2, "подпись", [], ["https://cdn.test/v.mp4"])
            )
            bot.send_video.side_effect = TelegramBadRequest(
                method=None,
                message="Bad Request: failed to get HTTP URL content",
            )
            with patch("preview.download_to_temp", new=AsyncMock(side_effect=RuntimeError("download_failed"))):
                await poller.poll_once()
            self.assertEqual(bot.send_video.await_count, 1)
            bot.send_message.assert_awaited_once_with("@dest", "подпись")
            self.assertEqual(stats.forwarded, 1)

    async def test_album_with_video_degrades_without_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(
                return_value=make_album_page(
                    2,
                    "подпись",
                    ["https://cdn.test/1.jpg"],
                    ["https://cdn.test/v.mp4"],
                )
            )
            error = TelegramBadRequest(
                method=None,
                message="Bad Request: failed to get HTTP URL content",
            )
            bot.send_media_group.side_effect = error
            with patch("preview.download_to_temp", new=AsyncMock(side_effect=RuntimeError("download_failed"))):
                await poller.poll_once()
            self.assertEqual(bot.send_video.await_count, 0)
            self.assertEqual(bot.send_photo.await_count, 1)
            self.assertEqual(bot.send_photo.await_args.kwargs["caption"], "подпись")
            self.assertEqual(stats.forwarded, 1)

    async def test_album_is_sent_as_media_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            page = make_page([(2, "", ["https://cdn.test/1.jpg", "https://cdn.test/2.jpg"])])
            poller._fetch = AsyncMock(return_value=page)
            await poller.poll_once()
            self.assertEqual(bot.send_media_group.await_count, 1)
            self.assertEqual(stats.forwarded, 1)

    async def test_dedup_same_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@chan", "@dest", via="preview")
            store.set_last_processed(route["id"], 1)
            forwarder, bot, stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            page = make_page([(2, "новое", [])])
            poller._fetch = AsyncMock(return_value=page)
            await poller.poll_once()
            await poller.poll_once()
            self.assertEqual(bot.send_message.await_count, 1)
            self.assertEqual(stats.forwarded, 1)

    async def test_bot_routes_are_not_polled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add_route("@chan", "@dest", via="bot")
            forwarder, _bot, _stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(return_value=make_page([(5, "x", [])]))
            await poller.poll_once()
            self.assertEqual(poller._fetch.await_count, 0)

    async def test_global_pause_stops_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add_route("@chan", "@dest", via="preview")
            store.set_enabled(False)
            forwarder, _bot, _stats = make_forwarder(store)
            poller = PreviewPoller(store=store, forwarder=forwarder)
            poller._fetch = AsyncMock(return_value=make_page([(5, "x", [])]))
            await poller.poll_once()
            self.assertEqual(poller._fetch.await_count, 0)


class TestPreviewCheckSource(unittest.IsolatedAsyncioTestCase):
    async def test_check_source_ok(self):
        with patch("preview.fetch_page", new=AsyncMock(return_value=make_page([(1, "x", [])]))):
            title, normalized = await preview.check_source("@chan", main.normalize_channel_input)
        self.assertEqual(normalized, "@chan")
        self.assertEqual(title, "@chan")

    async def test_check_source_requires_public(self):
        with self.assertRaises(ValueError) as context:
            await preview.check_source("-100123", main.normalize_channel_input)
        self.assertIn("публичный канал", str(context.exception))

    async def test_check_source_not_found(self):
        with (
            patch("preview.fetch_page", new=AsyncMock(return_value=None)),
            self.assertRaises(ValueError) as context,
        ):
            await preview.check_source("@missing", main.normalize_channel_input)
        self.assertIn("Не нашёл", str(context.exception))


class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status, chunks):
        self.status = status
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, status=200, chunks=()):
        self._status = status
        self._chunks = chunks

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url, proxy=None):
        return FakeResponse(self._status, self._chunks)


class TestDownload(unittest.IsolatedAsyncioTestCase):
    async def test_download_to_temp_writes_file(self):
        with patch("preview.aiohttp.ClientSession", new=FakeSession(chunks=[b"abc", b"def"])):
            path = await preview.download_to_temp("https://cdn.test/photo.jpg")
        try:
            self.assertTrue(Path(path).exists())
            self.assertEqual(Path(path).read_bytes(), b"abcdef")
        finally:
            preview.remove_file(path)

    async def test_download_to_temp_removes_file_on_error(self):
        with (
            patch("preview.aiohttp.ClientSession", new=FakeSession(status=404)),
            self.assertRaises(RuntimeError),
        ):
            await preview.download_to_temp("https://cdn.test/photo.jpg")


if __name__ == "__main__":
    unittest.main(verbosity=2)
