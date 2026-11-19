"""Sends queued notifications after the transaction has committed (best effort)."""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.i18n import t
from app.core.outbox import Notification

log = logging.getLogger(__name__)


def _markup(n: Notification) -> InlineKeyboardMarkup | None:
    if n.lead_id is not None:
        row = [
            InlineKeyboardButton(text=t(n.language, "btn_open_lead"), callback_data=f"l:view:{n.lead_id}"),
            InlineKeyboardButton(text=t(n.language, "btn_lead_contacted"), callback_data=f"l:set:{n.lead_id}:contacted"),
        ]
        return InlineKeyboardMarkup(inline_keyboard=[row])
    if n.invoice_id is not None:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t(n.language, "btn_open_invoice"), callback_data=f"i:view:{n.invoice_id}"),
        ]])
    if n.visit_id is not None:
        rows = []
        if n.action == "remind":
            rows.append([
                InlineKeyboardButton(text=t(n.language, "btn_visit_confirm"), callback_data=f"v:ok:{n.visit_id}"),
                InlineKeyboardButton(text=t(n.language, "btn_visit_change"), callback_data=f"v:chg:{n.visit_id}"),
            ])
            rows.append([InlineKeyboardButton(text=t(n.language, "btn_visit_cancel"), callback_data=f"v:cx:{n.visit_id}")])
        rows.append([InlineKeyboardButton(text=t(n.language, "btn_open_visit"), callback_data=f"v:view:{n.visit_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if n.contract_id is not None:
        row = []
        if n.action == "renew":
            row.append(InlineKeyboardButton(text=t(n.language, "btn_request_renewal"), callback_data=f"c:renew:{n.contract_id}"))
        row.append(InlineKeyboardButton(text=t(n.language, "btn_open_contract"), callback_data=f"c:view:{n.contract_id}"))
        return InlineKeyboardMarkup(inline_keyboard=[row])
    if n.ticket_id is None:
        return None
    if n.action == "feedback":
        label, data = "btn_send_feedback", f"t:feedback:{n.ticket_id}"
    else:
        label, data = "btn_open_ticket", f"t:view:{n.ticket_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t(n.language, label), callback_data=data)]])


async def deliver(bot: Bot, notifications: list[Notification]) -> None:
    """A failed delivery is logged and never breaks the action that caused it."""
    for n in notifications:
        try:
            await bot.send_message(n.telegram_id, t(n.language, n.key, **n.params), reply_markup=_markup(n))
        except TelegramForbiddenError:
            log.warning("Notification not delivered: user %s blocked the bot", n.telegram_id)
        except Exception:
            log.exception("Notification to %s failed", n.telegram_id)
