"""The repository's SQL, executed.

The unit tests assert the SQL's *shape* — that it says EXISTS, that the cursor
is a row-value comparison. These assert its *behaviour*, which is the part that
was wrong about identifier types while every shape test passed.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from api.pagination import Cursor, build_page
from api.repositories.books import BookFilters, authors_for, list_books

pytestmark = pytest.mark.integration


async def query(url: str, **kwargs: Any) -> list[Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await list_books(connection, **kwargs)
    finally:
        await engine.dispose()


class TestOrdering:
    async def test_books_come_back_in_case_insensitive_title_order(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # Ordering on raw title would put every capitalised title before every
        # lowercase one, which reads as randomly shuffled to a user.
        for title in ("banana", "Apple", "cherry"):
            await seeded.book(title)

        rows = await query(api_database_url, filters=BookFilters(), limit=10)

        assert [row.title for row in rows] == ["Apple", "banana", "cherry"]

    async def test_books_without_a_year_are_still_ordered(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The reason the sort key is the title, not the publication year.

        A third of the catalogue has no year; sorting on it would drop those
        books out of a stable ordering entirely.
        """
        await seeded.book("Dated", year=1965)
        await seeded.book("Undated", year=None)

        rows = await query(api_database_url, filters=BookFilters(), limit=10)

        assert [row.title for row in rows] == ["Dated", "Undated"]


class TestKeysetPagination:
    async def test_paging_returns_every_book_exactly_once(
        self, seeded: Any, api_database_url: str
    ) -> None:
        for index in range(10):
            await seeded.book(f"Book {index:02d}")

        seen: list[str] = []
        cursor: Cursor | None = None
        for _ in range(10):
            rows = await query(api_database_url, filters=BookFilters(), limit=3, cursor=cursor)
            page = build_page(
                rows, 3, cursor_of=lambda r: Cursor(sort_title=r.sort_title, book_id=r.id)
            )
            seen.extend(row.title for row in page.items)
            if not page.next_cursor:
                break
            cursor = Cursor(sort_title=page.items[-1].sort_title, book_id=page.items[-1].id)

        assert len(seen) == 10
        assert len(set(seen)) == 10

    async def test_a_book_inserted_mid_pagination_does_not_shift_the_page(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The failure offset pagination has and keyset does not.

        Under OFFSET, inserting a row earlier in the sort order pushes an
        unseen row past the offset and the client never sees it — silently.
        """
        for index in range(6):
            await seeded.book(f"Book {index:02d}")

        first = await query(api_database_url, filters=BookFilters(), limit=3)
        after = Cursor(sort_title=first[2].sort_title, book_id=first[2].id)

        # A book that sorts before everything already returned.
        await seeded.book("AAA inserted late")

        rest = await query(api_database_url, filters=BookFilters(), limit=10, cursor=after)

        titles = [row.title for row in rest]
        assert "AAA inserted late" not in titles  # it belongs on an earlier page
        assert titles == ["Book 03", "Book 04", "Book 05"]

    async def test_titles_that_collide_still_paginate(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # Title alone is not unique. Without the id in the key, a cursor
        # landing on a duplicate title would skip or repeat rows.
        for _ in range(5):
            await seeded.book("Identical")

        seen: list[int] = []
        cursor: Cursor | None = None
        for _ in range(5):
            rows = await query(api_database_url, filters=BookFilters(), limit=2, cursor=cursor)
            page = build_page(
                rows, 2, cursor_of=lambda r: Cursor(sort_title=r.sort_title, book_id=r.id)
            )
            seen.extend(row.id for row in page.items)
            if not page.next_cursor:
                break
            cursor = Cursor(sort_title=page.items[-1].sort_title, book_id=page.items[-1].id)

        assert sorted(seen) == sorted(set(seen))
        assert len(seen) == 5

    async def test_the_over_fetch_detects_another_page(
        self, seeded: Any, api_database_url: str
    ) -> None:
        for index in range(4):
            await seeded.book(f"Book {index}")

        rows = await query(api_database_url, filters=BookFilters(), limit=3)

        assert len(rows) == 4  # limit + 1


class TestFilters:
    async def test_an_author_matches_by_trigram(self, seeded: Any, api_database_url: str) -> None:
        # The operator that does not exist without pg_trgm.
        book_id = await seeded.book("Dune")
        await seeded.author(book_id, "Frank Herbert")
        await seeded.book("Unrelated")

        rows = await query(api_database_url, filters=BookFilters(author="Frank Herbert"), limit=10)

        assert [row.title for row in rows] == ["Dune"]

    async def test_a_book_with_several_matching_subjects_returns_once(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The one-to-many trap.

        A JOIN here returns the book once per matching subject, silently
        shortening the page. The semi-join is what prevents it.
        """
        book_id = await seeded.book("Dune")
        for subject in ("science fiction", "science fiction classics"):
            await seeded.subject(book_id, subject)

        rows = await query(
            api_database_url, filters=BookFilters(subject="science fiction"), limit=10
        )

        assert len(rows) == 1

    async def test_a_book_with_several_authors_returns_once(
        self, seeded: Any, api_database_url: str
    ) -> None:
        book_id = await seeded.book("Collaboration")
        await seeded.author(book_id, "Brian Herbert")
        await seeded.author(book_id, "Kevin Anderson")

        rows = await query(api_database_url, filters=BookFilters(author="Herbert"), limit=10)

        assert len(rows) == 1

    async def test_year_bounds_are_inclusive(self, seeded: Any, api_database_url: str) -> None:
        await seeded.book("Early", year=1960)
        await seeded.book("Middle", year=1965)
        await seeded.book("Late", year=1970)

        rows = await query(
            api_database_url, filters=BookFilters(year_from=1960, year_to=1965), limit=10
        )

        assert {row.title for row in rows} == {"Early", "Middle"}

    async def test_a_year_filter_excludes_books_without_a_year(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # NULL >= 1960 is NULL, not true. Worth pinning: it is the correct
        # behaviour and it surprises people.
        await seeded.book("Dated", year=1965)
        await seeded.book("Undated", year=None)

        rows = await query(api_database_url, filters=BookFilters(year_from=1900), limit=10)

        assert [row.title for row in rows] == ["Dated"]

    async def test_filters_combine(self, seeded: Any, api_database_url: str) -> None:
        wanted = await seeded.book("Wanted", year=1965, language="eng")
        await seeded.author(wanted, "Frank Herbert")
        other = await seeded.book("Wrong language", year=1965, language="fre")
        await seeded.author(other, "Frank Herbert")

        rows = await query(
            api_database_url,
            filters=BookFilters(author="Herbert", language="eng", year_from=1960),
            limit=10,
        )

        assert [row.title for row in rows] == ["Wanted"]


class TestRelationshipLoading:
    async def test_authors_load_for_a_whole_page_in_one_query(
        self, seeded: Any, api_database_url: str
    ) -> None:
        first = await seeded.book("First")
        second = await seeded.book("Second")
        await seeded.author(first, "Frank Herbert")
        await seeded.author(second, "Ursula Le Guin")

        engine = create_async_engine(api_database_url)
        try:
            async with engine.connect() as connection:
                grouped = await authors_for(connection, [first, second])
        finally:
            await engine.dispose()

        assert [a.name for a in grouped[first]] == ["Frank Herbert"]
        assert [a.name for a in grouped[second]] == ["Ursula Le Guin"]

    async def test_a_book_with_no_authors_is_absent_not_empty(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # The route uses .get(id, []) precisely because of this.
        book_id = await seeded.book("Anonymous")

        engine = create_async_engine(api_database_url)
        try:
            async with engine.connect() as connection:
                grouped = await authors_for(connection, [book_id])
        finally:
            await engine.dispose()

        assert grouped == {}
