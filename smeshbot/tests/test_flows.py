import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberMember,
    ChatMemberOwner,
    Message,
    Update,
    User,
)

import main

TEST_TOKEN = "123456789:" + "A" * 35
ADMIN_ID = 111
BOT_ID = 42


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []
        self.messages: list[Message] = []
        self.member_statuses: list[str] = []

    async def close(self) -> None:
        return None

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=BOT_ID, is_bot=True, first_name="Test", username="test_bot")
        if name == "GetChat":
            return Chat(id=-1001234567890, type="channel", title="Test Channel", username="test_channel")
        if name == "GetChatMember":
            status = self.member_statuses.pop(0) if self.member_statuses else "creator"
            if status == "member":
                return ChatMemberMember(
                    status="member",
                    user=User(id=BOT_ID, is_bot=True, first_name="Test"),
                )
            return ChatMemberOwner(
                status="creator",
                user=User(id=BOT_ID, is_bot=True, first_name="Test"),
                is_anonymous=False,
            )
        if name in {"SendMessage", "EditMessageText", "ForwardMessage", "CopyMessage"}:
            return self._message()
        if name == "SendMediaGroup":
            return [self._message()]
        if name in {"AnswerCallbackQuery", "DeleteWebhook", "GetUpdates"}:
            return True if name != "GetUpdates" else []
        return True

    def _message(self) -> Message:
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=ADMIN_ID, type="private", first_name="Admin"),
        )
        self.messages.append(message)
        return message

    def last(self, name: str) -> Any:
        for method in reversed(self.requests):
            if type(method).__name__ == name:
                return method
        raise AssertionError(f"Request {name} was not sent")

    def count(self, name: str) -> int:
        return sum(1 for method in self.requests if type(method).__name__ == name)


def button_data(markup) -> list[str]:
    result = []
    if markup is None:
        return result
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data:
                result.append(button.callback_data)
    return result


def user_message(update_id: int, user_id: int, text: str) -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private", first_name="Admin"),
            from_user=User(id=user_id, is_bot=False, first_name="Admin"),
            text=text,
        ),
    )


def callback_update(update_id: int, user_id: int, data: str, panel: Message) -> Update:
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=User(id=user_id, is_bot=False, first_name="Admin"),
            chat_instance="test",
            data=data,
            message=panel,
        ),
    )


class FlowTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / "config.json"
        self.store = main.ConfigStore(str(self.config_path))
        self.stats = main.Stats()
        self.session = FakeSession()
        self.bot = Bot(token=TEST_TOKEN, session=self.session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.addAsyncCleanup(self.bot.session.close)
        self.forwarder = main.Forwarder(
            bot=self.bot,
            store=self.store,
            stats=self.stats,
            recent=main.RecentTracker(),
            admin_id=ADMIN_ID,
        )
        self.dispatcher = Dispatcher(storage=MemoryStorage())
        self.dispatcher.include_router(main.build_admin_router(self.bot, self.store, self.stats, ADMIN_ID, BOT_ID))
        self.dispatcher.include_router(main.build_public_router())
        self.dispatcher.include_router(main.build_channel_router(self.forwarder))
        self.update_id = 100
        self.panel: Message | None = None

    def next_id(self) -> int:
        self.update_id += 1
        return self.update_id

    async def send_text(self, text: str, user_id: int = ADMIN_ID) -> None:
        await self.dispatcher.feed_update(self.bot, user_message(self.next_id(), user_id, text))

    async def press(self, data: str, user_id: int = ADMIN_ID) -> None:
        if self.panel is None:
            await self.start_panel()
        await self.dispatcher.feed_update(self.bot, callback_update(self.next_id(), user_id, data, self.panel))

    async def start_panel(self) -> None:
        await self.send_text("/start")
        self.panel = self.session.messages[-1]

    async def test_main_menu_and_all_buttons(self) -> None:
        await self.start_panel()
        start = self.session.last("SendMessage")
        self.assertIn("SMESH FORWARDER", start.text)
        self.assertIn("Статус", start.text)
        self.assertIn("Маршрутов", start.text)
        data = button_data(start.reply_markup)
        for expected in (
            "menu:sources",
            "menu:routes",
            "route:add",
            "menu:remove",
            "system:start",
            "system:pause",
            "menu:status",
            "menu:settings",
        ):
            self.assertIn(expected, data)

        await self.press("menu:sources")
        self.assertIn("Источники", self.session.last("EditMessageText").text)
        await self.press("menu:routes")
        self.assertIn("Маршруты", self.session.last("EditMessageText").text)
        await self.press("menu:status")
        status_text = self.session.last("EditMessageText").text
        self.assertIn("Получено сообщений", status_text)
        self.assertIn("Успешно отправлено", status_text)
        self.assertIn("Время работы", status_text)
        await self.press("status:refresh")
        self.assertEqual(self.session.last("AnswerCallbackQuery").text, "Обновлено")
        await self.press("menu:settings")
        self.assertIn("Настройки", self.session.last("EditMessageText").text)

        await self.press("settings:mode")
        self.assertEqual(self.store.mode, "copy")
        await self.press("settings:mode")
        self.assertEqual(self.store.mode, "forward")
        await self.press("settings:notify")
        self.assertFalse(self.store.notify_errors)
        await self.press("settings:notify")
        self.assertTrue(self.store.notify_errors)
        delays = [self.store.delay]
        for _ in range(4):
            await self.press("settings:delay")
            delays.append(self.store.delay)
        self.assertEqual(delays, [0, 1, 3, 5, 0])

        await self.press("system:pause")
        self.assertFalse(self.store.enabled)
        self.assertIn("Остановлен", self.session.last("EditMessageText").text)
        await self.press("system:start")
        self.assertTrue(self.store.enabled)

        await self.press("menu:remove")
        self.assertIn("Удалить маршрут", self.session.last("EditMessageText").text)

    async def test_add_route_flow(self) -> None:
        await self.start_panel()
        await self.press("route:add")
        self.assertIn("Шаг 1 из 2", self.session.last("SendMessage").text)

        await self.send_text("@toporlive")
        step_two = self.session.last("SendMessage")
        self.assertIn("Теперь напиши", step_two.text)
        self.assertEqual(self.session.last("GetChat").chat_id, "@toporlive")
        self.assertEqual(self.session.last("GetChatMember").user_id, BOT_ID)

        await self.send_text("@smesh_news")
        confirm = self.session.last("SendMessage")
        self.assertIn("Проверь, всё верно?", confirm.text)
        buttons = button_data(confirm.reply_markup)
        self.assertIn("route:create", buttons)
        self.assertIn("fsm:cancel", buttons)
        self.assertEqual(self.store.routes(), [])

        await self.press("route:create")
        self.assertEqual(len(self.store.routes()), 1)
        route = self.store.routes()[0]
        self.assertEqual(route["source"], "@toporlive")
        self.assertEqual(route["destination"], "@smesh_news")
        self.assertTrue(route["enabled"])
        self.assertIn("Пересылка включена", self.session.last("SendMessage").text)

        reloaded = main.ConfigStore(str(self.config_path))
        self.assertEqual(len(reloaded.routes()), 1)

    async def test_add_route_falls_back_to_preview(self) -> None:
        self.session.member_statuses = ["member", "creator"]
        with patch(
            "main.preview_check_source",
            new=AsyncMock(return_value=("Test Channel", "@toporlive")),
        ):
            await self.start_panel()
            await self.press("route:add")
            await self.send_text("@toporlive")
            await self.send_text("@smesh_news")
            await self.press("route:create")
        self.assertEqual(len(self.store.routes()), 1)
        route = self.store.routes()[0]
        self.assertEqual(route["via"], "preview")
        self.assertIn("Пересылка включена", self.session.last("SendMessage").text)

    async def test_add_route_cancel_does_not_create(self) -> None:
        await self.start_panel()
        await self.press("route:add")
        await self.send_text("@toporlive")
        await self.press("fsm:cancel")
        self.assertEqual(self.store.routes(), [])
        await self.send_text("@smesh_news")
        self.assertIn("Используйте /start", self.session.last("SendMessage").text)

    async def test_add_route_validation_errors(self) -> None:
        await self.start_panel()
        await self.press("route:add")
        await self.send_text("!!!")
        self.assertIn("Не понимаю формат", self.session.last("SendMessage").text)
        await self.send_text("@toporlive")
        await self.send_text("@toporlive")
        self.assertIn("один и тот же канал", self.session.last("SendMessage").text)
        self.assertEqual(self.store.routes(), [])

    async def test_duplicate_route_rejected(self) -> None:
        self.store.add_route("@toporlive", "@smesh_news")
        await self.start_panel()
        await self.press("route:add")
        await self.send_text("@toporlive")
        await self.send_text("@smesh_news")
        self.assertIn("уже есть", self.session.last("SendMessage").text)
        self.assertEqual(len(self.store.routes()), 1)

    async def test_route_management_buttons(self) -> None:
        self.store.add_route("@toporlive", "@smesh_news")
        await self.start_panel()

        await self.press("menu:routes")
        list_text = self.session.last("EditMessageText").text
        self.assertIn("@toporlive", list_text)
        self.assertIn("@smesh_news", list_text)
        buttons = button_data(self.session.last("EditMessageText").reply_markup)
        self.assertIn("route:open:1", buttons)
        self.assertIn("route:toggle:1", buttons)
        self.assertIn("route:del:1", buttons)

        await self.press("route:toggle:1")
        self.assertFalse(self.store.route("1")["enabled"])
        self.assertIn("Выключен", self.session.last("EditMessageText").text)
        await self.press("route:toggle:1")
        self.assertTrue(self.store.route("1")["enabled"])

        await self.press("route:open:1")
        card = self.session.last("EditMessageText")
        self.assertIn("Маршрут 1", card.text)
        card_buttons = button_data(card.reply_markup)
        self.assertIn("route:vtoggle:1", card_buttons)
        self.assertIn("route:setsrc:1", card_buttons)
        self.assertIn("route:setdst:1", card_buttons)
        self.assertIn("route:del:1", card_buttons)

        await self.press("route:vtoggle:1")
        self.assertFalse(self.store.route("1")["enabled"])
        await self.press("route:vtoggle:1")
        self.assertTrue(self.store.route("1")["enabled"])

        await self.press("route:setsrc:1")
        self.assertIn("ОТКУДА брать сообщения", self.session.last("SendMessage").text)
        await self.send_text("@other_channel")
        self.assertEqual(self.store.route("1")["source"], "@other_channel")

        await self.press("route:setdst:1")
        await self.send_text("@other_destination")
        self.assertEqual(self.store.route("1")["destination"], "@other_destination")

        await self.press("route:del:1")
        confirm_buttons = button_data(self.session.last("EditMessageText").reply_markup)
        self.assertIn("route:delconfirm:1", confirm_buttons)
        await self.press("route:delconfirm:1")
        self.assertEqual(self.store.routes(), [])

    async def test_source_management_buttons(self) -> None:
        self.store.add_route("@toporlive", "@smesh_news")
        self.store.add_route("@toporlive", "@smesh_fun")
        await self.start_panel()

        await self.press("menu:sources")
        self.assertIn("@toporlive", self.session.last("EditMessageText").text)
        buttons = button_data(self.session.last("EditMessageText").reply_markup)
        self.assertIn("src:open:@toporlive", buttons)

        await self.press("src:open:@toporlive")
        source_view = self.session.last("EditMessageText")
        self.assertIn("Маршрутов", source_view.text)
        source_buttons = button_data(source_view.reply_markup)
        self.assertIn("src:toggle:@toporlive", source_buttons)
        self.assertIn("src:del:@toporlive", source_buttons)

        await self.press("src:toggle:@toporlive")
        self.assertFalse(self.store.route("1")["enabled"])
        self.assertFalse(self.store.route("2")["enabled"])
        await self.press("src:toggle:@toporlive")
        self.assertTrue(self.store.route("1")["enabled"])
        self.assertTrue(self.store.route("2")["enabled"])

        await self.press("src:del:@toporlive")
        confirm_buttons = button_data(self.session.last("EditMessageText").reply_markup)
        self.assertIn("src:delconfirm:@toporlive", confirm_buttons)
        await self.press("src:delconfirm:@toporlive")
        self.assertEqual(self.store.routes(), [])

    async def test_non_admin_has_no_access(self) -> None:
        await self.start_panel()
        await self.send_text("/start", user_id=555)
        denied = self.session.last("SendMessage")
        self.assertIn("нет доступа", denied.text)
        before = self.session.count("EditMessageText")
        await self.press("menu:main", user_id=555)
        self.assertEqual(self.session.count("EditMessageText"), before)
        self.assertIn("нет доступа", self.session.last("AnswerCallbackQuery").text)

    async def test_unknown_callback_is_answered(self) -> None:
        await self.start_panel()
        await self.press("route:open:999")
        self.assertIn("уже удалён", self.session.last("AnswerCallbackQuery").text)

    async def test_settings_persist_across_restart(self) -> None:
        await self.start_panel()
        await self.press("settings:mode")
        await self.press("settings:notify")
        await self.press("settings:delay")
        await self.press("system:pause")
        reloaded = main.ConfigStore(str(self.config_path))
        self.assertEqual(reloaded.mode, "copy")
        self.assertFalse(reloaded.notify_errors)
        self.assertEqual(reloaded.delay, 1)
        self.assertFalse(reloaded.enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
