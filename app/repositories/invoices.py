from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, InvoiceCounter, InvoiceItem, User


class InvoiceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def next_seq(self, company_id: int, year: int) -> int:
        """The next invoice number of the year. The counter row is locked until the commit, so
        two invoices can never get the same number."""
        await self.session.execute(
            pg_insert(InvoiceCounter)
            .values(company_id=company_id, year=year, last_number=0)
            .on_conflict_do_nothing(index_elements=["company_id", "year"])
        )
        stmt = (
            select(InvoiceCounter)
            .where(InvoiceCounter.company_id == company_id, InvoiceCounter.year == year)
            .with_for_update()
        )
        counter = (await self.session.execute(stmt)).scalar_one()
        counter.last_number += 1
        await self.session.flush()
        return counter.last_number

    async def add(self, invoice: Invoice, items: list[InvoiceItem]) -> Invoice:
        self.session.add(invoice)
        await self.session.flush()
        for item in items:
            item.invoice_id = invoice.id
            self.session.add(item)
        await self.session.flush()
        return invoice

    async def get(self, company_id: int, invoice_id: int) -> Invoice | None:
        stmt = select(Invoice).where(Invoice.company_id == company_id, Invoice.id == invoice_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def items(self, invoice_id: int) -> list[InvoiceItem]:
        stmt = select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id).order_by(InvoiceItem.position)
        return list((await self.session.execute(stmt)).scalars())

    async def list_page(
        self, company_id: int, offset: int, limit: int, customer_id: int | None = None
    ) -> list[tuple[Invoice, User]]:
        stmt = (
            select(Invoice, User).join(User, Invoice.customer_id == User.id)
            .where(Invoice.company_id == company_id)
        )
        if customer_id is not None:
            stmt = stmt.where(Invoice.customer_id == customer_id)
        stmt = stmt.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).offset(offset).limit(limit)
        return [(i, u) for i, u in (await self.session.execute(stmt)).all()]

    async def count(self, company_id: int, customer_id: int | None = None) -> int:
        stmt = select(func.count()).select_from(Invoice).where(Invoice.company_id == company_id)
        if customer_id is not None:
            stmt = stmt.where(Invoice.customer_id == customer_id)
        return (await self.session.execute(stmt)).scalar_one()
