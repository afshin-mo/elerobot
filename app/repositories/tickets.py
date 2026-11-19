from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Building, Elevator, Ticket, TicketAttachment, TicketFeedback, TicketReport, TicketStatus,
    TicketStatusHistory,
)


class TicketRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, ticket: Ticket) -> Ticket:
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def get(self, company_id: int, ticket_id: int) -> Ticket | None:
        stmt = select(Ticket).where(Ticket.company_id == company_id, Ticket.id == ticket_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def _filters(self, company_id, statuses, customer_id, technician_id):
        conditions = [Ticket.company_id == company_id, Ticket.status.in_(statuses)]
        if customer_id is not None:
            conditions.append(Ticket.customer_id == customer_id)
        if technician_id is not None:
            conditions.append(Ticket.technician_id == technician_id)
        return conditions

    async def list_rows(
        self, company_id: int, statuses: Sequence[TicketStatus], *, offset: int, limit: int,
        customer_id: int | None = None, technician_id: int | None = None, newest_first: bool = True,
    ) -> list[tuple[Ticket, Elevator, Building]]:
        stmt = (
            select(Ticket, Elevator, Building)
            .join(Elevator, Ticket.elevator_id == Elevator.id)
            .join(Building, Elevator.building_id == Building.id)
            .where(*self._filters(company_id, statuses, customer_id, technician_id))
            .order_by(Ticket.id.desc() if newest_first else Ticket.id.asc())
            .offset(offset)
            .limit(limit)
        )
        return [tuple(row) for row in (await self.session.execute(stmt)).all()]

    async def count(
        self, company_id: int, statuses: Sequence[TicketStatus], *,
        customer_id: int | None = None, technician_id: int | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(Ticket).where(
            *self._filters(company_id, statuses, customer_id, technician_id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    # ---- history, attachments, reports ----

    async def add_history(
        self, company_id: int, ticket_id: int, from_status: TicketStatus | None,
        to_status: TicketStatus, changed_by_id: int, note: str | None = None,
    ) -> None:
        self.session.add(TicketStatusHistory(
            company_id=company_id, ticket_id=ticket_id, from_status=from_status,
            to_status=to_status, changed_by_id=changed_by_id, note=note,
        ))
        await self.session.flush()

    async def add_attachment(self, company_id: int, ticket_id: int, file_id: str) -> None:
        self.session.add(TicketAttachment(
            company_id=company_id, ticket_id=ticket_id, telegram_file_id=file_id
        ))
        await self.session.flush()

    async def list_attachments(self, company_id: int, ticket_id: int) -> list[str]:
        stmt = (
            select(TicketAttachment.telegram_file_id)
            .where(TicketAttachment.company_id == company_id, TicketAttachment.ticket_id == ticket_id)
            .order_by(TicketAttachment.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def add_report(self, report: TicketReport) -> TicketReport:
        self.session.add(report)
        await self.session.flush()
        return report

    async def list_reports(self, company_id: int, ticket_id: int) -> list[TicketReport]:
        stmt = (
            select(TicketReport)
            .where(TicketReport.company_id == company_id, TicketReport.ticket_id == ticket_id)
            .order_by(TicketReport.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_feedback(self, company_id: int, ticket_id: int) -> TicketFeedback | None:
        stmt = select(TicketFeedback).where(
            TicketFeedback.company_id == company_id, TicketFeedback.ticket_id == ticket_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_feedback(self, feedback: TicketFeedback) -> TicketFeedback:
        self.session.add(feedback)
        await self.session.flush()
        return feedback
