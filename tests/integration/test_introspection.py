"""Schema introspection and bounded SQL against the real schema.

The unit suite proves the parser rejects what it should. This one asks the
questions the parser cannot answer on its own: does the schema we describe
match the schema that exists, and does the database still refuse a write if the
parser is ever wrong?
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from api.repositories import introspection

pytestmark = pytest.mark.integration


@pytest.fixture
async def connect(api_database_url: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(api_database_url)
    try:
        yield engine.connect
    finally:
        await engine.dispose()


class TestDescribe:
    async def test_it_lists_the_queryable_tables(self, connect) -> None:  # type: ignore[no-untyped-def]
        async with connect() as connection:
            described = await introspection.describe(connection)

        # Every table we advertise must exist. A described table that isn't
        # there sends an agent to write a query that cannot run.
        assert set(described) == introspection.QUERYABLE_TABLES

    async def test_it_describes_columns_that_exist(self, connect) -> None:  # type: ignore[no-untyped-def]
        async with connect() as connection:
            described = await introspection.describe(connection)
            # The strongest available check: query every advertised column.
            for table, detail in described.items():
                columns = ", ".join(column["name"] for column in detail["columns"])
                await connection.execute(text(f"SELECT {columns} FROM {table} LIMIT 0"))

    async def test_it_omits_tables_we_do_not_expose(self, connect) -> None:  # type: ignore[no-untyped-def]
        async with connect() as connection:
            described = await introspection.describe(connection)

        assert "alembic_version" not in described
        assert "rejected_records" not in described


class TestRunQuery:
    async def test_it_returns_rows(self, connect, seeded) -> None:  # type: ignore[no-untyped-def]
        await seeded.book("Dune", year=1965)
        await seeded.book("Neuromancer", year=1984)

        async with connect() as connection:
            result = await introspection.run_query(
                connection, "SELECT title, published_year FROM books ORDER BY title"
            )

        assert result.columns == ["title", "published_year"]
        assert [row["title"] for row in result.rows] == ["Dune", "Neuromancer"]
        assert result.row_count == 2
        assert result.truncated is False

    async def test_an_aggregate_works(self, connect, seeded) -> None:  # type: ignore[no-untyped-def]
        await seeded.book("A", year=1965)
        await seeded.book("B", year=1965)
        await seeded.book("C", year=1984)

        async with connect() as connection:
            result = await introspection.run_query(
                connection,
                "SELECT published_year, count(*) AS n FROM books "
                "GROUP BY published_year ORDER BY published_year",
            )

        assert result.rows == [
            {"published_year": 1965, "n": 2},
            {"published_year": 1984, "n": 1},
        ]

    async def test_results_are_capped_and_say_so(self, connect, seeded, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(introspection, "MAX_ROWS", 2)
        for title in ("A", "B", "C", "D"):
            await seeded.book(title)

        async with connect() as connection:
            result = await introspection.run_query(connection, "SELECT title FROM books")

        # Silently returning 2 of 4 rows is worse than returning none: the
        # agent reports a total that is simply wrong.
        assert result.row_count == 2
        assert result.truncated is True

    async def test_a_rejected_query_never_reaches_the_database(self, connect) -> None:  # type: ignore[no-untyped-def]
        async with connect() as connection:
            with pytest.raises(introspection.QueryRejectedError):
                await introspection.run_query(connection, "DROP TABLE books")


class TestTheDatabaseIsTheRealBoundary:
    """Defence in depth: what holds when the parser is wrong.

    These tests bypass validate() on purpose. Its keyword rules are the first
    line and the one most likely to have a gap; the read-only transaction and
    the statement timeout are enforced by PostgreSQL and are what actually make
    an unauthenticated SQL endpoint survivable.
    """

    async def test_a_write_is_refused_even_if_validation_is_bypassed(
        self,
        connect,  # type: ignore[no-untyped-def]
        seeded,
        monkeypatch,
    ) -> None:
        monkeypatch.setattr(introspection, "validate", lambda sql: sql)

        with pytest.raises(Exception, match="read-only transaction"):
            async with connect() as connection:
                await introspection.run_query(
                    connection,
                    "INSERT INTO books (identity_key, title, content_hash) VALUES ('x', 'x', 'x')",
                )

    async def test_a_non_catalogue_table_is_refused_by_the_role(
        self,
        connect,  # type: ignore[no-untyped-def]
        monkeypatch,
    ) -> None:
        """The layer that holds when the allowlist is wrong.

        rejected_records is a real table in this database, holds raw source
        payloads, and is not queryable. If the parser ever misses it,
        PostgreSQL must still refuse.
        """
        monkeypatch.setattr(introspection, "validate", lambda sql: sql)

        with pytest.raises(Exception, match="permission denied"):
            async with connect() as connection:
                await introspection.run_query(
                    connection,
                    "SELECT * FROM rejected_records",
                    readonly_role="catalogue_readonly",
                )

    async def test_a_missing_role_fails_closed(self, connect) -> None:  # type: ignore[no-untyped-def]
        # Running with the service's own grants because the restricted role is
        # absent is the silent downgrade the whole layer exists to prevent.
        with pytest.raises(introspection.QueryRejectedError, match="not available"):
            async with connect() as connection:
                await introspection.run_query(
                    connection,
                    "SELECT title FROM books",
                    readonly_role="role_that_does_not_exist",
                )

    async def test_the_role_still_reads_the_catalogue(self, connect, seeded) -> None:  # type: ignore[no-untyped-def]
        # A boundary nobody can work inside gets worked around.
        await seeded.book("Dune")

        async with connect() as connection:
            result = await introspection.run_query(
                connection, "SELECT title FROM books", readonly_role="catalogue_readonly"
            )

        assert result.rows == [{"title": "Dune"}]

    async def test_a_role_name_that_is_not_an_identifier_is_refused(self, connect) -> None:  # type: ignore[no-untyped-def]
        # The name is interpolated into SET ROLE, which cannot be
        # parameterised, so a misconfigured value must not reach the database.
        with pytest.raises(introspection.QueryRejectedError, match="not a valid identifier"):
            async with connect() as connection:
                await introspection.run_query(
                    connection,
                    "SELECT title FROM books",
                    readonly_role='readonly"; DROP TABLE books; --',
                )

    async def test_a_slow_query_is_cut_off(self, connect, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        monkeypatch.setattr(introspection, "validate", lambda sql: sql)
        monkeypatch.setattr(introspection, "STATEMENT_TIMEOUT_MS", 250)

        # pg_sleep(30) would hold a worker for thirty seconds; the timeout is
        # what stops one query from being a denial of service.
        with pytest.raises(Exception, match=r"statement timeout|canceling"):
            async with connect() as connection:
                await introspection.run_query(connection, "SELECT pg_sleep(30)")
