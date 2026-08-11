"""The contract, against the schema the pipeline actually builds.

The unit tests check the contract logic with fakes. This checks the thing the
logic is about: that what we require is what upstream ships.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from api.schema_contract import REQUIRED_SCHEMA, verify_schema
from tests.integration.conftest import PINNED_ALEMBIC_REVISION

pytestmark = [pytest.mark.integration, pytest.mark.contract]

Scalar = Callable[[str], Awaitable[Any]]
Scalars = Callable[[str], Awaitable[list[Any]]]


class TestPin:
    async def test_the_migrations_land_on_the_pinned_revision(self, scalar: Scalar) -> None:
        """The pin is what turns a moving upstream into a visible failure.

        Without it this suite silently tracks the pipeline's head, and the day
        a migration changes something we read, the failure surfaces in an
        unrelated pull request.
        """
        revision = await scalar("SELECT version_num FROM alembic_version")

        assert revision == PINNED_ALEMBIC_REVISION


class TestContractHolds:
    async def test_every_required_object_exists_upstream(self, api_database_url: str) -> None:
        engine = create_async_engine(api_database_url)
        try:
            async with engine.connect() as connection:
                result = await verify_schema(connection)
        finally:
            await engine.dispose()

        assert result.compatible, result.describe()


class TestAssumptionsTheApiReliesOn:
    """Schema features the API's SQL depends on but does not itself create."""

    async def test_pg_trgm_is_installed(self, scalar: Scalar) -> None:
        # Without it `a.name % :author` is not merely slow — it does not parse,
        # so every author and series filter is a 500.
        assert await scalar("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'") == 1

    async def test_the_trigram_operator_works(self, scalar: Scalar) -> None:
        assert await scalar("SELECT 'Frank Herbert' % 'herbert'") is True

    async def test_search_vector_is_generated_not_written(self, scalar: Scalar) -> None:
        # Neither repository writes it. If it stopped being generated, search
        # would silently return nothing rather than fail.
        generated = await scalar(
            """
            SELECT is_generated FROM information_schema.columns
            WHERE table_name = 'books' AND column_name = 'search_vector'
            """
        )

        assert generated == "ALWAYS"

    async def test_the_title_keyset_index_exists(self, scalars: Scalars) -> None:
        # Pagination orders by (lower(title), id). Without this index that
        # ordering sorts the whole table on every page.
        indexes = await scalars("SELECT indexdef FROM pg_indexes WHERE tablename = 'books'")

        assert any("lower(title" in definition for definition in indexes), indexes

    async def test_provenance_carries_a_source_per_book(self, scalars: Scalars) -> None:
        # get_book_provenance is the MCP surface's reason to exist; it reads
        # this table, so its shape is part of the contract.
        columns = await scalars(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'book_sources'
            """
        )

        assert {"book_id", "source", "source_id"} <= set(columns)


class TestContractIsNotOverPinned:
    async def test_we_require_less_than_the_pipeline_ships(self, scalar: Scalar) -> None:
        """Pinning everything would make every upstream migration a false alarm.

        The contract names what we read and stops there.
        """
        upstream = await scalar(
            """
            SELECT count(DISTINCT table_name) FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            """
        )

        assert len(REQUIRED_SCHEMA) < upstream
