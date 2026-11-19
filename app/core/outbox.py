"""Notification intents. Services emit them; the bot layer sends them after the commit.

This keeps services independent of Telegram (NFR-004) and makes sure nobody is told about
something that was rolled back.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Notification:
    telegram_id: int
    language: str
    key: str  # i18n key of the message text
    params: dict = field(default_factory=dict)
    ticket_id: int | None = None
    action: str = "view"  # "view" opens the ticket card, "feedback" opens the rating screen
    lead_id: int | None = None  # set for new consultation requests (managers only)
    contract_id: int | None = None  # contract messages; action renew adds a renewal button
    visit_id: int | None = None  # service visit messages; action remind adds the customer's buttons
    invoice_id: int | None = None  # invoice messages (an open button is added)


class Outbox:
    def __init__(self) -> None:
        self.items: list[Notification] = []

    def add(self, notification: Notification) -> None:
        self.items.append(notification)

    def clear(self) -> None:
        self.items.clear()


current_outbox: ContextVar[Outbox | None] = ContextVar("current_outbox", default=None)
