import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import main


def make_store(tmp_path):
    return main.ConfigStore(str(Path(tmp_path) / "config.json"))


def make_message(message_id=1, username="toporlive", media_group_id=None):
    chat = SimpleNamespace(id=-100111, username=username, type="channel")
    return SimpleNamespace(
        message_id=message_id,
        chat=chat,
        from_user=None,
        media_group_id=media_group_id,
        photo=None,
        video=None,
        document=None,
        audio=None,
        caption=None,
    )


def make_media_message(message_id, media_group_id="g1"):
    message = make_message(message_id=message_id, media_group_id=media_group_id)
    message.photo = [SimpleNamespace(file_id=f"file-{message_id}")]
    return message


def make_forwarder(store):
    bot = SimpleNamespace(
        id=42,
        forward_message=AsyncMock(),
        copy_message=AsyncMock(),
        send_message=AsyncMock(),
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


class TestConfigStore(unittest.TestCase):
    def test_creates_config_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = main.ConfigStore(str(path))
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["enabled"])
            self.assertEqual(data["routes"], [])
            self.assertEqual(store.mode, "forward")
            self.assertEqual(store.delay, 0)
            self.assertTrue(store.notify_errors)

    def test_add_route_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            self.assertEqual(route["id"], "1")
            self.assertTrue(route["enabled"])
            second = store.add_route("@moscowach", "@smesh_news")
            self.assertEqual(second["id"], "2")
            reloaded = main.ConfigStore(str(Path(tmp) / "config.json"))
            self.assertEqual(len(reloaded.routes()), 2)
            self.assertEqual(reloaded.route("1")["source"], "@toporlive")
            self.assertEqual(reloaded.route("2")["destination"], "@smesh_news")

    def test_delete_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            self.assertTrue(store.delete_route(route["id"]))
            self.assertFalse(store.delete_route(route["id"]))
            self.assertEqual(store.routes(), [])
            reloaded = main.ConfigStore(str(Path(tmp) / "config.json"))
            self.assertEqual(reloaded.routes(), [])

    def test_enable_disable_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            store.update_route(route["id"], enabled=False)
            self.assertFalse(store.route(route["id"])["enabled"])
            store.update_route(route["id"], enabled=True)
            self.assertTrue(store.route(route["id"])["enabled"])
            reloaded = main.ConfigStore(str(Path(tmp) / "config.json"))
            self.assertTrue(reloaded.route(route["id"])["enabled"])

    def test_source_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add_route("@toporlive", "@smesh_news")
            store.add_route("@toporlive", "@smesh_fun")
            store.add_route("@moscowach", "@smesh_news")
            self.assertEqual(len(store.sources()), 2)
            changed = store.set_source_enabled("@toporlive", False)
            self.assertEqual(changed, 2)
            self.assertFalse(store.route("1")["enabled"])
            self.assertFalse(store.route("2")["enabled"])
            self.assertTrue(store.route("3")["enabled"])
            removed = store.delete_source("@toporlive")
            self.assertEqual(removed, 2)
            self.assertEqual(len(store.routes()), 1)

    def test_corrupted_config_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ это не json", encoding="utf-8")
            store = main.ConfigStore(str(path))
            self.assertEqual(store.routes(), [])
            self.assertTrue(store.enabled)
            backups = list(Path(tmp).glob("config.json.broken-*"))
            self.assertEqual(len(backups), 1)

    def test_partial_config_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"routes": [{"source": "@a_channel", "destination": "@b_channel"}]}),
                encoding="utf-8",
            )
            store = main.ConfigStore(str(path))
            routes = store.routes()
            self.assertEqual(len(routes), 1)
            self.assertEqual(routes[0]["id"], "1")
            self.assertTrue(routes[0]["enabled"])
            self.assertEqual(routes[0]["last_processed_message_id"], 0)
            self.assertEqual(store.delay, 0)
            self.assertEqual(store.mode, "forward")

    def test_duplicate_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            self.assertTrue(store.duplicate_exists("@toporlive", "@smesh_news"))
            self.assertTrue(store.duplicate_exists("@TOporlive", "@smesh_news"))
            self.assertFalse(store.duplicate_exists("@toporlive", "@smesh_news", ignore_id=route["id"]))
            self.assertFalse(store.duplicate_exists("@toporlive", "@other"))

    def test_routes_for_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add_route("@toporlive", "@smesh_news")
            store.add_route("-100111", "@smesh_fun")
            self.assertEqual(len(store.routes_for_source(-100111, "toporlive")), 2)
            self.assertEqual(len(store.routes_for_source(-100222, None)), 0)
            self.assertEqual(len(store.routes_for_source(-100111, "unknown")), 1)

    def test_settings_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.set_mode("copy")
            store.set_notify_errors(False)
            store.set_delay(3)
            store.set_enabled(False)
            reloaded = main.ConfigStore(str(Path(tmp) / "config.json"))
            self.assertEqual(reloaded.mode, "copy")
            self.assertFalse(reloaded.notify_errors)
            self.assertEqual(reloaded.delay, 3)
            self.assertFalse(reloaded.enabled)


class TestValidation(unittest.TestCase):
    def test_normalize_valid(self):
        self.assertEqual(main.normalize_channel_input(" @Toporlive "), "@Toporlive")
        self.assertEqual(main.normalize_channel_input("toporlive"), "@toporlive")
        self.assertEqual(main.normalize_channel_input("https://t.me/toporlive"), "@toporlive")
        self.assertEqual(main.normalize_channel_input("t.me/toporlive/"), "@toporlive")
        self.assertEqual(main.normalize_channel_input("-1001234567890"), "-1001234567890")

    def test_normalize_invalid(self):
        for value in ("", "!!!", "+invite", "ab"):
            with self.assertRaises(ValueError):
                main.normalize_channel_input(value)

    def test_admin_filter(self):
        admin_filter = main.AdminFilter(123)
        allowed = SimpleNamespace(from_user=SimpleNamespace(id=123))
        denied = SimpleNamespace(from_user=SimpleNamespace(id=124))
        anonymous = SimpleNamespace(from_user=None)
        self.assertTrue(admin_filter(allowed))
        self.assertFalse(admin_filter(denied))
        self.assertFalse(admin_filter(anonymous))

    def test_destination_chat_id(self):
        self.assertEqual(main.destination_chat_id("@smesh_news"), "@smesh_news")
        self.assertEqual(main.destination_chat_id("-100123"), -100123)
        self.assertEqual(main.destination_chat_id("123"), 123)


class TestForwarder(unittest.TestCase):
    def test_forward_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            asyncio.run(forwarder._deliver_one(route, make_message(10)))
            self.assertEqual(bot.forward_message.await_count, 1)
            self.assertEqual(bot.copy_message.await_count, 0)
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.errors, 0)

    def test_copy_mode_uses_copy_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.set_mode("copy")
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            asyncio.run(forwarder._deliver_one(route, make_message(11)))
            self.assertEqual(bot.copy_message.await_count, 1)
            self.assertEqual(bot.forward_message.await_count, 0)
            self.assertEqual(stats.forwarded, 1)

    def test_restricted_falls_back_to_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            error = TelegramBadRequest(method=None, message="Bad Request: CHAT_FORWARDS_RESTRICTED")
            bot.forward_message.side_effect = error
            asyncio.run(forwarder._deliver_one(route, make_message(12)))
            self.assertEqual(bot.forward_message.await_count, 1)
            self.assertEqual(bot.copy_message.await_count, 1)
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.errors, 0)

    def test_restricted_both_fail_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            error = TelegramBadRequest(method=None, message="Bad Request: CHAT_FORWARDS_RESTRICTED")
            bot.forward_message.side_effect = error
            bot.copy_message.side_effect = error
            asyncio.run(forwarder._deliver_one(route, make_message(13)))
            self.assertEqual(stats.forwarded, 0)
            self.assertEqual(stats.errors, 1)
            self.assertEqual(bot.send_message.await_count, 1)

    def test_forbidden_reports_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            bot.forward_message.side_effect = TelegramForbiddenError(
                method=None, message="Forbidden: bot is not a member"
            )
            asyncio.run(forwarder._deliver_one(route, make_message(14)))
            self.assertEqual(stats.errors, 1)
            self.assertEqual(bot.send_message.await_count, 1)

    def test_flood_wait_retries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            bot.forward_message.side_effect = [
                TelegramRetryAfter(method=None, message="Flood control", retry_after=1),
                None,
            ]
            with patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
                asyncio.run(forwarder._deliver_one(route, make_message(15)))
            self.assertEqual(bot.forward_message.await_count, 2)
            self.assertEqual(stats.forwarded, 1)
            self.assertEqual(stats.errors, 0)
            sleep_mock.assert_awaited_once_with(2)

    def test_delay_setting_is_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.set_delay(3)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, _bot, stats = make_forwarder(store)
            with patch.object(main.asyncio, "sleep", new=AsyncMock()) as sleep_mock:
                asyncio.run(forwarder._deliver_one(route, make_message(16)))
            sleep_mock.assert_awaited_once_with(3)
            self.assertEqual(stats.forwarded, 1)

    def test_error_notification_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            bot.forward_message.side_effect = TelegramForbiddenError(method=None, message="Forbidden")
            asyncio.run(forwarder._deliver_one(route, make_message(17)))
            asyncio.run(forwarder._deliver_one(route, make_message(18)))
            self.assertEqual(stats.errors, 2)
            self.assertEqual(bot.send_message.await_count, 1)

    def test_album_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.set_mode("copy")
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            messages = [make_media_message(20), make_media_message(21)]
            handled = asyncio.run(forwarder._deliver_album_copy(route, messages))
            self.assertTrue(handled)
            self.assertEqual(bot.send_media_group.await_count, 1)
            self.assertEqual(stats.forwarded, 2)

    def test_unknown_source_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add_route("@toporlive", "@smesh_news")
            forwarder, bot, stats = make_forwarder(store)
            asyncio.run(forwarder.handle_incoming(make_message(30, username="unknown")))
            self.assertEqual(stats.received, 0)
            self.assertEqual(bot.forward_message.await_count, 0)

    def test_global_pause_ignores_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add_route("@toporlive", "@smesh_news")
            store.set_enabled(False)
            forwarder, bot, stats = make_forwarder(store)
            asyncio.run(forwarder.handle_incoming(make_message(31)))
            self.assertEqual(stats.received, 0)
            self.assertEqual(bot.forward_message.await_count, 0)

    def test_mark_processed_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            forwarder, _bot, stats = make_forwarder(store)
            message = make_message(40)
            self.assertTrue(forwarder.mark_processed(route, message.message_id))
            self.assertFalse(forwarder.mark_processed(route, message.message_id))
            self.assertEqual(stats.skipped, 1)
            self.assertEqual(store.route(route["id"])["last_processed_message_id"], 40)


class TestStatsAndHelpers(unittest.TestCase):
    def test_counters_and_uptime(self):
        stats = main.Stats()
        stats.add_received()
        stats.add_forwarded(2)
        stats.add_error("ошибка")
        stats.add_skipped()
        self.assertEqual(stats.received, 1)
        self.assertEqual(stats.forwarded, 2)
        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.skipped, 1)
        self.assertIn("сек", stats.uptime())

    def test_recent_tracker(self):
        tracker = main.RecentTracker(max_size=3, ttl=1.0)
        for message_id in (1, 2, 3, 4):
            tracker.add("1", message_id)
        self.assertTrue(tracker.seen("1", 4))
        self.assertTrue(tracker.seen("1", 3))
        self.assertFalse(tracker.seen("1", 1))
        time.sleep(1.1)
        self.assertFalse(tracker.seen("1", 3))

    def test_human_error(self):
        self.assertIn("не разрешил", main.human_error(RuntimeError("Bad Request: CHAT_FORWARDS_RESTRICTED")))
        self.assertIn("доступа", main.human_error(RuntimeError("Forbidden: bot is not a member")))
        self.assertIn("подождать", main.human_error(RuntimeError("Flood control: retry_after")))
        self.assertIn("лог", main.human_error(RuntimeError("something strange")))

    def test_redaction_filter(self):
        record = logging.LogRecord("t", logging.INFO, "", 0, "token abc:XYZ proxy pass123", None, None)
        main.SecretRedactionFilter(["abc:XYZ", "pass123"]).filter(record)
        message = record.getMessage()
        self.assertNotIn("abc:XYZ", message)
        self.assertNotIn("pass123", message)
        self.assertIn("***", message)

    def test_proxy_url_from_env(self):
        os.environ["PROXY_URL"] = "  http://user:pass@host:8080  "
        try:
            self.assertEqual(main.get_proxy_url(), "http://user:pass@host:8080")
        finally:
            os.environ.pop("PROXY_URL", None)
        self.assertEqual(main.get_proxy_url(), "")

    def test_text_builders_do_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            route = store.add_route("@toporlive", "@smesh_news")
            stats = main.Stats()
            for text in (
                main.panel_text(store),
                main.sources_text(store),
                main.source_text(store, "@toporlive"),
                main.routes_text(store),
                main.route_text(route),
                main.status_text(store, stats),
                main.settings_text(store),
            ):
                self.assertIsInstance(text, str)
                self.assertTrue(text)

    def test_callback_data_fits_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            long_name = "@" + "a" * 32
            store.add_route(long_name, "@" + "b" * 32)
            store.add_route("-1001234567890", "@smesh_news")
            routes = store.routes()
            markups = [
                main.main_menu(),
                main.sources_keyboard(store),
                main.routes_keyboard(store),
                main.status_keyboard(),
                main.settings_keyboard(store),
                main.route_keyboard(routes[0]),
                main.source_keyboard(store, routes[0]["source"]),
                main.confirm_route_delete_keyboard(routes[0]["id"]),
                main.confirm_source_delete_keyboard(routes[0]["source"]),
                main.add_confirm_keyboard(),
                main.back_button(),
                main.cancel_button(),
            ]
            for markup in markups:
                for row in markup.inline_keyboard:
                    for button in row:
                        self.assertLessEqual(
                            len(button.callback_data.encode("utf-8")),
                            64,
                            button.callback_data,
                        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
