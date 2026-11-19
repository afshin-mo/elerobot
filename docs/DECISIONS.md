# Decisions

## Approved by owner
- Python + aiogram; PostgreSQL; SQLAlchemy 2 + Alembic; Long Polling; JSON translation files.
- Modular monolith: bot / services / repositories / models+core.
- Hosting: Hetzner VPS with Docker (deploy only). Local dev/test: plain PostgreSQL, no Docker/VPS.
- `company_id` on every table except `companies`; full multi-tenancy not implemented.
- Manager-only close; customer feedback optional; manager can reject a report and reassign technician.
- Roles: manager, technician, customer. Login by phone number registered by a manager.

## Implementation decisions (low risk, changeable)
- Ticket number = `tickets.id` (no per-company counter yet).
- Enums stored as VARCHAR + CHECK constraint, not native PG enums.
- No ORM relationships yet; added when services need them.
- Docker files postponed to stage 6 (nothing long-running exists before stage 2).
