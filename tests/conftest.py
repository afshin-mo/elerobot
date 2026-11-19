import os
import subprocess
import sys

import pytest
from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

TEST_URL = os.environ.get("TEST_DATABASE_URL") or dotenv_values(".env").get("TEST_DATABASE_URL")


def _alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": TEST_URL}
    subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=env)


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Build the test schema from the real migrations (also tests the migrations)."""
    if not TEST_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")
    _alembic("check")  # fails if models and migrations drifted apart
    yield


@pytest.fixture
async def session():
    """Session inside a transaction that is always rolled back."""
    engine = create_async_engine(TEST_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(conn, join_transaction_mode="create_savepoint") as s:
            yield s
        await trans.rollback()
    await engine.dispose()
