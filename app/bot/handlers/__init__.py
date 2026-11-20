from aiogram import Router

from app.bot import flow
from app.bot.handlers import contracts, emergency, guest, invoices, leads, manager, start, tickets, visits


def build_routers() -> list[Router]:
    # emergency.build_router() must come before start.build_router() (it handles the QR deep link);
    # start.build_router() must stay last: it holds the catch-all fallback handler.
    return [
        flow.build_router(), manager.build_router(), tickets.build_router(),
        leads.build_router(), contracts.build_router(), visits.build_router(),
        invoices.build_router(), emergency.build_router(), guest.build_router(),
        start.build_router(),
    ]
