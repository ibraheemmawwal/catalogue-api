"""Series queries.

A series is the one place this catalogue routinely holds a claim it is not sure
about. Goodreads states positions on a series page; everywhere else they are
inferred from a title pattern. Both end up in ``book_series``, distinguished
only by ``confirmed`` — so every read here carries that flag rather than
flattening it into a number that looks equally authoritative.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection


async def get_series(connection: AsyncConnection, *, series_id: int) -> Row[Any] | None:
    result = await connection.execute(
        text("SELECT id, name FROM series WHERE id = :series_id"),
        {"series_id": series_id},
    )
    return result.first()


async def find_series_by_name(connection: AsyncConnection, *, name: str) -> Row[Any] | None:
    """The closest series by trigram similarity.

    Present for the MCP surface: an agent asks for "the Dune series", not for
    series 41. Ordered by similarity so the best match wins rather than
    whichever row the planner reached first.
    """
    result = await connection.execute(
        text(
            """
            SELECT id, name
            FROM series
            WHERE name % :name
            ORDER BY similarity(name, :name) DESC, id
            LIMIT 1
            """
        ),
        {"name": name},
    )
    return result.first()


async def members_of(connection: AsyncConnection, *, series_id: int) -> list[Row[Any]]:
    """Books in a series, in reading order.

    ``NULLS LAST`` matters: a book whose position nobody stated should sit
    after the ordered ones, not sort to the front and imply it comes first.
    The position is NUMERIC upstream, so a novella published as 4.5 lands
    between 4 and 5 rather than being rounded into one of them.
    """
    result = await connection.execute(
        text(
            """
            SELECT b.id, b.isbn13, b.title, b.published_year, b.language,
                   bs.position, bs.confirmed
            FROM book_series AS bs
            JOIN books AS b ON b.id = bs.book_id
            WHERE bs.series_id = :series_id
            ORDER BY bs.position ASC NULLS LAST, lower(b.title), b.id
            """
        ),
        {"series_id": series_id},
    )
    return list(result)
