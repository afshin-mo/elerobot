import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    Building, Company, Elevator, Role, Ticket, TicketFeedback, TicketStatus, User,
)


async def _seed(session, phone="+9647500000001"):
    company = Company(name="Test Co")
    session.add(company)
    await session.flush()
    customer = User(company_id=company.id, phone=phone, name="Ali", role=Role.CUSTOMER)
    session.add(customer)
    await session.flush()
    building = Building(company_id=company.id, owner_id=customer.id, name="Tower A", address="Erbil")
    session.add(building)
    await session.flush()
    elevator = Elevator(company_id=company.id, building_id=building.id, name="E1")
    session.add(elevator)
    await session.flush()
    return company, customer, elevator


async def test_create_ticket_chain_and_defaults(session):
    company, customer, elevator = await _seed(session)
    ticket = Ticket(
        company_id=company.id, elevator_id=elevator.id, customer_id=customer.id, description="stuck"
    )
    session.add(ticket)
    await session.flush()
    loaded = (await session.execute(select(Ticket).where(Ticket.id == ticket.id))).scalar_one()
    assert loaded.status is TicketStatus.NEW
    assert loaded.created_at is not None
    assert company.default_language == "ckb" and company.active_languages == ["ckb", "fa", "en"]


async def test_phone_unique_per_company_only(session):
    company, _, _ = await _seed(session)
    other = Company(name="Other Co")
    session.add(other)
    await session.flush()
    # same phone in another company is allowed
    session.add(User(company_id=other.id, phone="+9647500000001", name="X", role=Role.CUSTOMER))
    await session.flush()
    # same phone in the same company is not
    session.add(User(company_id=company.id, phone="+9647500000001", name="Y", role=Role.CUSTOMER))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_rating_must_be_1_to_5(session):
    company, customer, elevator = await _seed(session)
    ticket = Ticket(
        company_id=company.id, elevator_id=elevator.id, customer_id=customer.id, description="d"
    )
    session.add(ticket)
    await session.flush()
    session.add(TicketFeedback(company_id=company.id, ticket_id=ticket.id, rating=6))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_invalid_status_rejected_by_database(session):
    company, customer, elevator = await _seed(session)
    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO tickets (company_id, elevator_id, customer_id, description, status)"
                " VALUES (:c, :e, :u, 'd', 'bogus')"
            ),
            {"c": company.id, "e": elevator.id, "u": customer.id},
        )
