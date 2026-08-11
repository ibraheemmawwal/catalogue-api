"""Series endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import ConnectionDep
from api.errors import not_found
from api.repositories import books as book_repo
from api.repositories import series as repo
from api.schemas.books import BookSummary
from api.schemas.series import Series, SeriesMember

router = APIRouter(prefix="/v1/series", tags=["series"])


@router.get("/{series_id}", response_model=Series, summary="Get a series")
async def get_series(connection: ConnectionDep, series_id: int) -> Series:
    """A series and its books, in reading order."""
    row = await repo.get_series(connection, series_id=series_id)
    if row is None:
        raise not_found(
            "Series",
            str(series_id),
            hint="Search /v1/books/search by series name to find its identifier.",
        )

    members = await repo.members_of(connection, series_id=series_id)
    authors = await book_repo.authors_for(connection, [member.id for member in members])

    return Series(
        id=row.id,
        name=row.name,
        members=[
            SeriesMember(
                book=BookSummary(
                    id=member.id,
                    isbn13=member.isbn13,
                    title=member.title,
                    authors=[a.name for a in authors.get(member.id, [])],
                    published_year=member.published_year,
                    language=member.language,
                ),
                position=member.position,
                confirmed=member.confirmed,
            )
            for member in members
        ],
        confirmed_positions=sum(1 for member in members if member.confirmed),
    )
