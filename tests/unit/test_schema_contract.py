"""The contract against a schema this service does not own."""

from __future__ import annotations

from typing import Any

from api.schema_contract import REQUIRED_SCHEMA, ContractResult, verify_schema


class FakeResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class FakeConnection:
    """Returns whichever columns the test says the database has."""

    def __init__(self, present: dict[str, set[str]]) -> None:
        self._present = present
        self.queries = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> FakeResult:
        self.queries += 1
        return FakeResult(
            [(table, column) for table, columns in self._present.items() for column in columns]
        )


def everything() -> dict[str, set[str]]:
    return {table: set(columns) for table, columns in REQUIRED_SCHEMA.items()}


class TestVerification:
    async def test_a_matching_schema_is_compatible(self) -> None:
        result = await verify_schema(FakeConnection(everything()))  # type: ignore[arg-type]

        assert result.compatible
        assert result.describe() == "compatible"

    async def test_a_missing_table_is_named(self) -> None:
        present = everything()
        del present["book_sources"]

        result = await verify_schema(FakeConnection(present))  # type: ignore[arg-type]

        assert not result.compatible
        assert "book_sources" in result.describe()

    async def test_a_missing_column_is_named_with_its_table(self) -> None:
        # An operator reading this needs to know where to look; "incompatible"
        # alone sends them to the wrong repository.
        present = everything()
        present["books"].discard("isbn13")

        result = await verify_schema(FakeConnection(present))  # type: ignore[arg-type]

        assert "books.isbn13" in result.describe()

    async def test_extra_columns_upstream_are_not_a_failure(self) -> None:
        # The pipeline evolves independently. Only what we read is pinned —
        # otherwise every unrelated migration raises a false alarm here, and an
        # alarm that cries wolf gets muted.
        present = everything()
        present["books"].add("some_new_pipeline_column")

        assert (await verify_schema(FakeConnection(present))).compatible  # type: ignore[arg-type]

    async def test_it_asks_once_regardless_of_table_count(self) -> None:
        # This runs on every readiness probe; one query per table is a probe
        # that fails under the load it is meant to report on.
        connection = FakeConnection(everything())

        await verify_schema(connection)  # type: ignore[arg-type]

        assert connection.queries == 1


class TestContractContent:
    def test_provenance_tables_are_pinned(self) -> None:
        # The MCP surface's reason to exist. If book_sources vanished upstream,
        # get_book_provenance would fail at request time rather than at startup.
        assert "book_sources" in REQUIRED_SCHEMA

    def test_series_position_and_confirmation_are_pinned(self) -> None:
        # get_series distinguishes stated from inferred order; both columns are
        # load-bearing for that claim.
        assert {"position", "confirmed"} <= REQUIRED_SCHEMA["book_series"]

    def test_the_search_vector_is_pinned(self) -> None:
        assert "search_vector" in REQUIRED_SCHEMA["books"]


class TestDescription:
    def test_it_reports_both_kinds_of_gap(self) -> None:
        result = ContractResult(
            compatible=False,
            missing_tables=("series",),
            missing_columns=("books.title",),
        )

        described = result.describe()

        assert "series" in described
        assert "books.title" in described
