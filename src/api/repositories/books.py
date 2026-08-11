"""Book queries.

Two shapes of read, and the difference between them is deliberate:

*Collections* select from ``books`` alone, then fetch relationships for the
page's IDs in a second round trip. Joining authors and subjects into the main
query would multiply rows before ``LIMIT`` and return fewer books than asked
for — the classic one-to-many pagination bug, and it only shows up for books
with several authors.

*Detail* reads one book, where that concern does not arise.

Filters are appended conditionally rather than guarded with
``:value IS NULL OR column = :value``. The guarded form is one statement and
reads well, but it defeats the index: the planner cannot know the parameter is
null, so it plans for the general case and scans.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from api.pagination import Cursor
from api.schemas.books import AuthorRef, Book, BookSummary, SeriesRef

# Selected by name rather than *: a new pipeline column should not silently
# widen every response here.
_COLLECTION_COLUMNS = """
    b.id, b.isbn13, b.title, b.published_year, b.language, lower(b.title) AS sort_title
"""

_DETAIL_COLUMNS = """
    b.id, b.isbn13, b.title, b.subtitle, b.published_year, b.publisher,
    b.page_count, b.language, b.cover_url, b.goodreads_average_rating,
    b.download_count
"""


class BookFilters:
    """The filters a collection read accepts.

    A small object rather than eight parameters threaded through three
    functions — and it keeps the SQL-building in one place where the
    conditional appends can be read together.
    """

    def __init__(
        self,
        *,
        author: str | None = None,
        subject: str | None = None,
        series: str | None = None,
        language: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
    ) -> None:
        self.author = author
        self.subject = subject
        self.series = series
        self.language = language
        self.year_from = year_from
        self.year_to = year_to

    def clauses(self) -> tuple[list[str], dict[str, Any]]:
        """SQL fragments and their parameters."""
        parts: list[str] = []
        params: dict[str, Any] = {}

        if self.language is not None:
            parts.append("b.language = :language")
            params["language"] = self.language
        if self.year_from is not None:
            parts.append("b.published_year >= :year_from")
            params["year_from"] = self.year_from
        if self.year_to is not None:
            parts.append("b.published_year <= :year_to")
            params["year_to"] = self.year_to

        # Semi-joins, not joins: a book with three matching subjects must be
        # returned once, not three times.
        #
        # The trigram operator is a single %. text() binds with :name, so it
        # never needs the %% escaping that raw DBAPI pyformat does — escaping
        # it sends `%%` to the server, where no such operator exists.
        if self.author is not None:
            parts.append(
                """EXISTS (
                    SELECT 1 FROM book_authors ba
                    JOIN authors a ON a.id = ba.author_id
                    WHERE ba.book_id = b.id AND a.name % :author
                )"""
            )
            params["author"] = self.author
        if self.subject is not None:
            parts.append(
                """EXISTS (
                    SELECT 1 FROM book_subjects bs
                    JOIN subjects s ON s.id = bs.subject_id
                    WHERE bs.book_id = b.id AND s.normalized_name = :subject
                )"""
            )
            params["subject"] = self.subject
        if self.series is not None:
            parts.append(
                """EXISTS (
                    SELECT 1 FROM book_series bse
                    JOIN series se ON se.id = bse.series_id
                    WHERE bse.book_id = b.id AND se.name % :series
                )"""
            )
            params["series"] = self.series

        return parts, params


async def list_books(
    connection: AsyncConnection,
    *,
    filters: BookFilters,
    limit: int,
    cursor: Cursor | None = None,
) -> list[Row[Any]]:
    """One page of books, over-fetched by one.

    The extra row is how the caller learns whether another page exists without
    a second COUNT over the same predicate.
    """
    clauses, params = filters.clauses()

    if cursor is not None:
        # Row-value comparison, matching the (lower(title), id) index exactly.
        # Spelling it as an OR of two comparisons would produce the same rows
        # and lose the index.
        clauses.append("(lower(b.title), b.id) > (:cursor_title, :cursor_id)")
        params["cursor_title"] = cursor.sort_title
        params["cursor_id"] = cursor.book_id

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params["limit_plus_one"] = limit + 1

    result = await connection.execute(
        text(
            f"""
            SELECT {_COLLECTION_COLUMNS}
            FROM books AS b
            {where}
            ORDER BY lower(b.title), b.id
            LIMIT :limit_plus_one
            """
        ),
        params,
    )
    return list(result)


async def authors_for(
    connection: AsyncConnection, book_ids: Sequence[int]
) -> dict[int, list[AuthorRef]]:
    """Authors for a page of books, in one round trip.

    Called once per page rather than once per book: the per-book form is
    correct and turns a 20-item page into 21 queries.
    """
    if not book_ids:
        return {}

    result = await connection.execute(
        text(
            """
            SELECT ba.book_id, a.id, a.name
            FROM book_authors AS ba
            JOIN authors AS a ON a.id = ba.author_id
            WHERE ba.book_id = ANY(:book_ids)
            ORDER BY a.name
            """
        ),
        {"book_ids": list(book_ids)},
    )

    grouped: dict[int, list[AuthorRef]] = {}
    for book_id, author_id, name in result:
        grouped.setdefault(book_id, []).append(AuthorRef(id=author_id, name=name))
    return grouped


async def get_book(connection: AsyncConnection, *, isbn13: str) -> Row[Any] | None:
    """One book by canonical identity."""
    result = await connection.execute(
        text(f"SELECT {_DETAIL_COLUMNS} FROM books AS b WHERE b.isbn13 = :isbn13"),
        {"isbn13": isbn13},
    )
    return result.first()


async def get_book_by_id(connection: AsyncConnection, *, book_id: int) -> Row[Any] | None:
    """One book by internal identifier.

    Present because search and the MCP tools hand back IDs for books that have
    no ISBN-13 — a fallback identity is still an identity.
    """
    result = await connection.execute(
        text(f"SELECT {_DETAIL_COLUMNS} FROM books AS b WHERE b.id = :book_id"),
        {"book_id": book_id},
    )
    return result.first()


async def subjects_for(connection: AsyncConnection, book_id: int) -> list[str]:
    result = await connection.execute(
        text(
            """
            SELECT s.name
            FROM book_subjects AS bs
            JOIN subjects AS s ON s.id = bs.subject_id
            WHERE bs.book_id = :book_id
            ORDER BY s.name
            """
        ),
        {"book_id": book_id},
    )
    return [row[0] for row in result]


async def series_for(connection: AsyncConnection, book_id: int) -> list[SeriesRef]:
    result = await connection.execute(
        text(
            """
            SELECT se.id, se.name, bse.position, bse.confirmed
            FROM book_series AS bse
            JOIN series AS se ON se.id = bse.series_id
            WHERE bse.book_id = :book_id
            ORDER BY se.name
            """
        ),
        {"book_id": book_id},
    )
    return [SeriesRef(id=row[0], name=row[1], position=row[2], confirmed=row[3]) for row in result]


def to_summary(row: Row[Any], authors: list[AuthorRef]) -> BookSummary:
    return BookSummary(
        id=row.id,
        isbn13=row.isbn13,
        title=row.title,
        authors=[author.name for author in authors],
        published_year=row.published_year,
        language=row.language,
    )


def to_book(
    row: Row[Any],
    *,
    authors: list[AuthorRef],
    subjects: list[str],
    series: list[SeriesRef],
) -> Book:
    return Book(
        id=row.id,
        isbn13=row.isbn13,
        title=row.title,
        subtitle=row.subtitle,
        authors=authors,
        subjects=subjects,
        series=series,
        published_year=row.published_year,
        publisher=row.publisher,
        page_count=row.page_count,
        language=row.language,
        cover_url=row.cover_url,
        goodreads_average_rating=row.goodreads_average_rating,
        download_count=row.download_count,
    )
