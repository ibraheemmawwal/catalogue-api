"""The search endpoint.

Registered before ``/v1/books/{isbn13}``. Without that ordering the literal
path is captured by the parameterised route and every search is rejected as a
malformed ISBN.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from api.deps import ConnectionDep, StateDep
from api.errors import invalid_request
from api.pagination import InvalidCursorError, build_page
from api.repositories import books as book_repo
from api.repositories import search as search_repo
from api.schemas.books import BookSummary
from api.search_cursor import SearchCursor, decode_search_cursor, query_fingerprint

router = APIRouter(prefix="/v1/books", tags=["books"])


class SearchPage(BaseModel):
    """Search results, with the ranking that produced them."""

    model_config = ConfigDict(frozen=True)

    items: list[BookSummary]
    next_cursor: str | None = None
    mode: str = Field(
        description=(
            "'fulltext' when the query matched the search index, 'similarity' "
            "when it fell back to fuzzy matching — usually a misspelling."
        )
    )


@router.get("/search", response_model=SearchPage, summary="Search books")
async def search_books(
    connection: ConnectionDep,
    state: StateDep,
    q: Annotated[str, Query(min_length=2, max_length=200, description="Search terms.")],
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> SearchPage:
    """Full-text search, falling back to fuzzy matching."""
    query = q.strip()
    if len(query) < 2:
        # Length is validated before trimming, so "  a  " passes the check and
        # arrives here as a one-character query.
        raise invalid_request("Search terms must be at least 2 characters after trimming.")

    page_size = limit or state.settings.default_page_size

    decoded: SearchCursor | None = None
    if cursor is not None:
        try:
            decoded = decode_search_cursor(cursor, query=query)
        except InvalidCursorError as error:
            raise invalid_request(f"The cursor could not be read ({error}).") from error

    rows, mode = await search_repo.search(connection, query=query, limit=page_size, cursor=decoded)

    page = build_page(
        rows,
        page_size,
        cursor_of=lambda row: SearchCursor(
            mode=mode,
            score=row.rank,
            book_id=row.id,
            query_hash=query_fingerprint(query),
        ),
    )
    authors = await book_repo.authors_for(connection, [row.id for row in page.items])

    return SearchPage(
        items=[book_repo.to_summary(row, authors.get(row.id, [])) for row in page.items],
        next_cursor=page.next_cursor,
        mode=mode.value,
    )
