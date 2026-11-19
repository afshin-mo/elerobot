from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.core.i18n import t


def language_keyboard(languages: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=t(code, "language_name"), callback_data=f"lang:{code}")]
        for code in languages
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def phone_keyboard(lang: str) -> ReplyKeyboardMarkup:
    button = KeyboardButton(text=t(lang, "share_phone_button"), request_contact=True)
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)
