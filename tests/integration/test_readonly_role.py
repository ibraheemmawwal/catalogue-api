"""The boundary that does not depend on the parser being right.

``run_sql`` validates a caller's query before running it, and the existing
tool tests prove that allowlist works. They cannot prove what happens when it
does not. The first draft of that parser had four known evasions, so the
interesting question is not whether it currently rejects ``DELETE`` — it is
what PostgreSQL does with a statement that reaches it anyway.

So nothing here goes through the parser. Every test switches to
``catalogue_readonly`` and issues the statement directly, which is the
situation a parser bug creates. What holds at that point is the grant.

The division of labour is deliberate and worth keeping straight: the parser
keeps system catalogues out, because a role that can read ``pg_catalog`` is
normal PostgreSQL and not something a GRANT can fix. This role keeps *our*
non-public tables out, which is the half a parser evasion would otherwise
expose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

# Exactly the grant in scripts/sql/readonly_role.sql. Duplicated on purpose:
# if the grant changes, this list has to change with it, and that is the point
# at which someone decides whether widening it was intended.
READABLE = (
    "books",
    "authors",
    "book_authors",
    "subjects",
    "book_subjects",
    "series",
    "book_series",
    "book_sources",
    "ingestion_runs",
)

# Catalogue tables the API never exposes. rejected_records holds records that
# failed validation, including the payloads that caused it; resolution_attempts
# holds what was tried against which source and why it failed. Both are
# operational detail about the pipeline, not published catalogue data.
WITHHELD = (
    "rejected_records",
    "resolution_attempts",
    "source_runs",
)


@pytest.fixture
def as_readonly(api_database_url: str) -> Callable[[str], Awaitable[Any]]:
    """Run one statement as ``catalogue_readonly``.

    A fresh transaction per statement, because a refused statement aborts the
    one it was issued in and every later assertion in a shared transaction
    would fail for the wrong reason.
    """

    async def run(sql: str) -> Any:
        engine = create_async_engine(api_database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text('SET LOCAL ROLE "catalogue_readonly"'))
                return (await connection.execute(text(sql))).all()
        finally:
            await engine.dispose()

    return run


@pytest.fixture
async def a_new_table(api_database_url: str) -> AsyncIterator[str]:
    """A table created after the grants were applied."""
    engine = create_async_engine(api_database_url)
    name = "arrived_later"
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE {name} (secret text)"))
            await connection.execute(text(f"INSERT INTO {name} VALUES ('do not read me')"))
        yield name
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
        await engine.dispose()


class TestWhatItMayRead:
    @pytest.mark.parametrize("table", READABLE)
    async def test_each_granted_table_is_readable(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]], table: str
    ) -> None:
        # The other half of the boundary. A role that refused everything would
        # pass every test below and make run_sql useless.
        await as_readonly(f"SELECT * FROM {table} LIMIT 1")


class TestWhatItMayNotRead:
    @pytest.mark.parametrize("table", WITHHELD)
    async def test_a_withheld_catalogue_table_is_refused(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]], table: str
    ) -> None:
        with pytest.raises(ProgrammingError, match="permission denied"):
            await as_readonly(f"SELECT * FROM {table}")

    async def test_a_table_added_later_is_not_readable_by_default(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]], a_new_table: str
    ) -> None:
        """The grant must not extend itself to whatever arrives next.

        This is the failure that would never be noticed: a migration adds a
        table, nobody revisits the role, and it is readable because someone
        once wrote ALTER DEFAULT PRIVILEGES. The grant is table-by-table
        precisely so a new table is closed until a person opens it.
        """
        with pytest.raises(ProgrammingError, match="permission denied"):
            await as_readonly(f"SELECT * FROM {a_new_table}")

    async def test_the_role_list_is_not_readable(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]]
    ) -> None:
        # pg_authid holds password hashes and is superuser-only. The parser
        # blocks pg_ names before this matters; this is what remains if it
        # does not.
        with pytest.raises(ProgrammingError, match="permission denied"):
            await as_readonly("SELECT * FROM pg_authid")


class TestWhatItMayNotDo:
    @pytest.mark.parametrize(
        "statement",
        [
            "INSERT INTO books (identity_key, title, content_hash) VALUES ('x', 'x', 'x')",
            "UPDATE books SET title = 'rewritten'",
            "DELETE FROM books",
            "TRUNCATE books",
        ],
        ids=["insert", "update", "delete", "truncate"],
    )
    async def test_a_write_is_refused(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]], statement: str
    ) -> None:
        """Refused by the grant alone.

        run_sql also opens its transaction READ ONLY, which would stop these
        on its own. That is defence in depth and this test deliberately does
        not use it: the grant has to be sufficient by itself, or removing the
        READ ONLY line one day would quietly remove the protection.
        """
        with pytest.raises(ProgrammingError, match="permission denied"):
            await as_readonly(statement)

    async def test_it_cannot_create_a_table(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]]
    ) -> None:
        # CREATE on the public schema is granted to PUBLIC by default in older
        # PostgreSQL, and revoking it is why this passes rather than an
        # accident of version.
        with pytest.raises(ProgrammingError, match="permission denied"):
            await as_readonly("CREATE TABLE smuggled (x int)")

    async def test_it_cannot_grant_itself_more(
        self, seeded: Any, as_readonly: Callable[[str], Awaitable[Any]]
    ) -> None:
        with pytest.raises(ProgrammingError):
            await as_readonly("GRANT ALL ON rejected_records TO catalogue_readonly")
