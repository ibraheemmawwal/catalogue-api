"""Book endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import ConnectionDep, StateDep
from api.errors import invalid_request, not_found
from api.pagination import Cursor, InvalidCursorError, build_page, decode_cursor
from api.repositories import books as repo
from api.schemas.books import Book, BookPage
from api.validators import normalise_isbn13

router = APIRouter(prefix="/v1/books", tags=["books"])


@router.get("", response_model=BookPage, summary="List books")
async def list_books(
    connection: ConnectionDep,
    state: StateDep,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page.")] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    author: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    subject: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    series: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    language: Annotated[str | None, Query(pattern=r"^[a-z]{3}$")] = None,
    year_from: Annotated[int | None, Query(ge=1400, le=2100)] = None,
    year_to: Annotated[int | None, Query(ge=1400, le=2100)] = None,
) -> BookPage:
    """A filtered page of books, ordered by title."""
    if year_from is not None and year_to is not None and year_from > year_to:
        # Caught here rather than returning an empty page: an empty result for
        # an impossible range looks like "no such books" and sends the caller
        # looking for a data problem that does not exist.
        raise invalid_request(
            f"year_from ({year_from}) is after year_to ({year_to}); no book can match."
        )

    page_size = limit or state.settings.default_page_size

    decoded: Cursor | None = None
    if cursor is not None:
        try:
            decoded = decode_cursor(cursor)
        except InvalidCursorError as error:
            raise invalid_request(
                f"The cursor could not be read ({error}). Request the first page without a cursor."
            ) from error

    rows = await repo.list_books(
        connection,
        filters=repo.BookFilters(
            author=author,
            subject=subject,
            series=series,
            language=language,
            year_from=year_from,
            year_to=year_to,
        ),
        limit=page_size,
        cursor=decoded,
    )

    page = build_page(
        rows,
        page_size,
        cursor_of=lambda row: Cursor(sort_title=row.sort_title, book_id=row.id),
    )
    authors = await repo.authors_for(connection, [row.id for row in page.items])

    return BookPage(
        items=[repo.to_summary(row, authors.get(row.id, [])) for row in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/{isbn13}", response_model=Book, summary="Get a book by ISBN-13")
async def get_book(connection: ConnectionDep, isbn13: str) -> Book:
    """One book by canonical identity."""
    normalised = normalise_isbn13(isbn13)
    if normalised is None:
        raise invalid_request(
            f"{isbn13!r} is not a valid ISBN-13. Expected 13 digits with a correct "
            "check digit; hyphens and spaces are allowed."
        )

    row = await repo.get_book(connection, isbn13=normalised)
    if row is None:
        raise not_found(
            "Book",
            normalised,
            hint="Search /v1/books/search to find its identifier.",
        )

    return repo.to_book(
        row,
        authors=(await repo.authors_for(connection, [row.id])).get(row.id, []),
        subjects=await repo.subjects_for(connection, row.id),
        series=await repo.series_for(connection, row.id),
    )
