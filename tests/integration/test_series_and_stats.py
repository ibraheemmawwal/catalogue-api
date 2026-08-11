"""Series ordering and catalogue statistics, executed."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from api.repositories import series as series_repo
from api.repositories import stats as stats_repo

pytestmark = pytest.mark.integration


async def run(url: str, fn: Any, **kwargs: Any) -> Any:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await fn(connection, **kwargs)
    finally:
        await engine.dispose()


class TestSeriesOrdering:
    async def test_members_come_back_in_reading_order(
        self, seeded: Any, api_database_url: str
    ) -> None:
        first = await seeded.book("Dune")
        third = await seeded.book("Children of Dune")
        second = await seeded.book("Dune Messiah")
        series_id = await seeded.series(first, "Dune", position="1", confirmed=True)
        await seeded.series(second, "Dune", position="2", confirmed=True)
        await seeded.series(third, "Dune", position="3", confirmed=True)

        members = await run(api_database_url, series_repo.members_of, series_id=series_id)

        assert [m.title for m in members] == ["Dune", "Dune Messiah", "Children of Dune"]

    async def test_a_fractional_position_sits_between_whole_ones(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """A novella published as 4.5 belongs between 4 and 5.

        An integer column would have rounded it into one of them, silently
        reordering the series.
        """
        fourth = await seeded.book("Book Four")
        novella = await seeded.book("The Novella")
        fifth = await seeded.book("Book Five")
        series_id = await seeded.series(fourth, "Saga", position="4", confirmed=True)
        await seeded.series(novella, "Saga", position="4.5", confirmed=True)
        await seeded.series(fifth, "Saga", position="5", confirmed=True)

        members = await run(api_database_url, series_repo.members_of, series_id=series_id)

        assert [m.title for m in members] == ["Book Four", "The Novella", "Book Five"]
        assert members[1].position == Decimal("4.5")

    async def test_a_book_with_no_position_sorts_last(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # NULLS FIRST is PostgreSQL's default for ASC. Without NULLS LAST an
        # unplaced book would sort to the front and imply it comes first.
        placed = await seeded.book("Placed")
        unplaced = await seeded.book("Unplaced")
        series_id = await seeded.series(placed, "Saga", position="1", confirmed=True)
        await seeded.series(unplaced, "Saga", position=None)

        members = await run(api_database_url, series_repo.members_of, series_id=series_id)

        assert [m.title for m in members] == ["Placed", "Unplaced"]

    async def test_confirmation_survives_the_read(self, seeded: Any, api_database_url: str) -> None:
        # A stated position and a guessed one are different claims.
        stated = await seeded.book("Stated")
        guessed = await seeded.book("Guessed")
        series_id = await seeded.series(stated, "Saga", position="1", confirmed=True)
        await seeded.series(guessed, "Saga", position="2", confirmed=False)

        members = await run(api_database_url, series_repo.members_of, series_id=series_id)

        assert [m.confirmed for m in members] == [True, False]


class TestSeriesLookup:
    async def test_a_series_is_found_by_approximate_name(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # An agent asks for "the Dune series", not for series 41.
        book_id = await seeded.book("Dune")
        await seeded.series(book_id, "Dune Chronicles", position="1", confirmed=True)

        row = await run(api_database_url, series_repo.find_series_by_name, name="dune chronicle")

        assert row is not None
        assert row.name == "Dune Chronicles"

    async def test_an_unknown_name_finds_nothing(self, seeded: Any, api_database_url: str) -> None:
        assert await run(api_database_url, series_repo.find_series_by_name, name="zzzz") is None


class TestStats:
    async def test_coverage_counts_populated_fields(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("With ISBN", isbn13="9780553380163", year=1965)
        await seeded.book("Without", isbn13=None, year=None)

        row = await run(api_database_url, stats_repo.coverage)

        assert row.books == 2
        assert row.with_isbn == 1
        assert row.with_year == 1

    async def test_the_year_range_reflects_the_data(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Old", year=1818)
        await seeded.book("New", year=2020)
        await seeded.book("Undated", year=None)

        row = await run(api_database_url, stats_repo.coverage)

        assert (row.earliest_year, row.latest_year) == (1818, 2020)

    async def test_an_empty_catalogue_reports_zero_not_an_error(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # A freshly deployed instance hits this before the first run.
        row = await run(api_database_url, stats_repo.coverage)

        assert row.books == 0
        assert row.earliest_year is None

    async def test_source_contributions_are_counted(
        self, seeded: Any, api_database_url: str
    ) -> None:
        book_id = await seeded.book("Dune")
        await seeded.source(book_id, "goodreads")
        await seeded.source(book_id, "openlibrary")
        other = await seeded.book("Other")
        await seeded.source(other, "goodreads")

        rows = await run(api_database_url, stats_repo.per_source)

        assert {row.source: row.books for row in rows} == {"goodreads": 2, "openlibrary": 1}

    async def test_contributions_overlap_by_design(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """A book resolved by three sources counts under all three.

        The question is what each source gave us, not how to apportion credit.
        """
        book_id = await seeded.book("Dune")
        for source in ("goodreads", "openlibrary", "googlebooks"):
            await seeded.source(book_id, source)

        rows = await run(api_database_url, stats_repo.per_source)

        assert sum(row.books for row in rows) == 3

    async def test_relationship_counts(self, seeded: Any, api_database_url: str) -> None:
        book_id = await seeded.book("Dune")
        await seeded.author(book_id, "Frank Herbert")
        await seeded.subject(book_id, "science fiction")
        await seeded.series(book_id, "Dune", position="1", confirmed=True)

        row = await run(api_database_url, stats_repo.relationship_counts)

        assert (row.authors, row.subjects, row.series) == (1, 1, 1)

    async def test_no_runs_yet_is_not_an_error(self, seeded: Any, api_database_url: str) -> None:
        assert await run(api_database_url, stats_repo.last_successful_run) is None
