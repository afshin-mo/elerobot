"""Fake Telegram plumbing so bot flows run end to end without a network."""
from datetime import datetime

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import GetMe, SendMessage
from aiogram.client.session.base import BaseSession
from aiogram.types import CallbackQuery, Chat, Contact, Message, MessageEntity, PhotoSize, Update
from aiogram.types import User as TgUser

TG_ID = 4242


class FakeSession(BaseSession):
    """Records the text of every outgoing message, edit and callback answer."""

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []
        self.sent_to: list[tuple[int | None, str]] = []  # (chat id, text)
        self.blocked: set[int] = set()  # chats whose user "blocked the bot"

    def last_to(self, chat_id: int) -> str:
        return [text for chat, text in self.sent_to if chat == chat_id][-1]

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return TgUser(id=999, is_bot=True, first_name="Bot", username="TestBot")
        if isinstance(method, SendMessage) and method.chat_id in self.blocked:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was blocked by the user")
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if text:
            self.sent.append(text)
            self.sent_to.append((getattr(method, "chat_id", None), text))
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


class Inject(BaseMiddleware):
    """Gives handlers the test's rolled-back session instead of a real one."""

    def __init__(self, session):
        self.session = session

    async def __call__(self, handler, event, data):
        data["session"] = self.session
        return await handler(event, data)


def _tg_user(tg_id, language_code=None, username=None):
    return TgUser(id=tg_id, is_bot=False, first_name="Ali", last_name="T",
                  language_code=language_code, username=username)


def _message(tg_id, language_code=None, username=None, **kw):
    return Message(
        message_id=1, date=datetime.now(), chat=Chat(id=tg_id, type="private"),
        from_user=_tg_user(tg_id, language_code, username), **kw,
    )


def text_update(text, n, tg_id=TG_ID, language_code=None, username=None):
    entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))] if text.startswith("/") else None
    return Update(update_id=n, message=_message(tg_id, language_code, username, text=text, entities=entities))


def lang_update(code, n, tg_id=TG_ID):
    return button_update(f"lang:{code}", n, tg_id)


def button_update(data, n, tg_id=TG_ID):
    cb = CallbackQuery(id="1", from_user=_tg_user(tg_id), chat_instance="x", data=data,
                       message=_message(tg_id, text="menu"))
    return Update(update_id=n, callback_query=cb)


def contact_update(phone, n, tg_id=TG_ID, user_id=None):
    contact = Contact(phone_number=phone, first_name="Ali", user_id=user_id or tg_id)
    return Update(update_id=n, message=_message(tg_id, contact=contact))


def photo_update(file_id, n, tg_id=TG_ID):
    photo = [PhotoSize(file_id=file_id, file_unique_id="u" + file_id, width=10, height=10)]
    return Update(update_id=n, message=_message(tg_id, photo=photo))
