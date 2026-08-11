"""Catalogue statistics.

Coverage is reported rather than assumed. A catalogue assembled from four
sources of differing completeness has real holes — a third of books have no
publication year — and a consumer deciding whether this data suits their
purpose needs the holes stated, not discovered.

One query, not six. These figures are read together and each extra round trip
is a full scan of the same table.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

_COVERAGE = """
SELECT
    count(*)                                              AS books,
    count(*) FILTER (WHERE isbn13 IS NOT NULL)            AS with_isbn,
    count(*) FILTER (WHERE published_year IS NOT NULL)    AS with_year,
    count(*) FILTER (WHERE publisher IS NOT NULL)         AS with_publisher,
    count(*) FILTER (WHERE page_count IS NOT NULL)        AS with_page_count,
    count(*) FILTER (WHERE cover_url IS NOT NULL)         AS with_cover,
    count(*) FILTER (WHERE goodreads_average_rating IS NOT NULL) AS with_rating,
    min(published_year)                                   AS earliest_year,
    max(published_year)                                   AS latest_year
FROM books
"""


async def coverage(connection: AsyncConnection) -> Row[Any]:
    result = await connection.execute(text(_COVERAGE))
    return result.one()


async def per_source(connection: AsyncConnection) -> list[Row[Any]]:
    """How many books each source contributed.

    The counts overlap deliberately: a book resolved by three sources appears
    under all three, because the question is "what did this source give us",
    not "how do we apportion credit".
    """
    result = await connection.execute(
        text(
            """
            SELECT source, count(DISTINCT book_id) AS books
            FROM book_sources
            GROUP BY source
            ORDER BY books DESC, source
            """
        )
    )
    return list(result)


async def relationship_counts(connection: AsyncConnection) -> Row[Any]:
    result = await connection.execute(
        text(
            """
            SELECT
                (SELECT count(*) FROM authors)  AS authors,
                (SELECT count(*) FROM subjects) AS subjects,
                (SELECT count(*) FROM series)   AS series
            """
        )
    )
    return result.one()


async def last_successful_run(connection: AsyncConnection) -> Row[Any] | None:
    """When the catalogue was last updated, and by how much.

    Reported because staleness is the failure a consumer cannot see from the
    data itself: a catalogue that stopped updating six months ago looks exactly
    like one that updated this morning.
    """
    result = await connection.execute(
        text(
            """
            SELECT started_at, records_loaded
            FROM ingestion_runs
            WHERE status IN ('success', 'partial_success')
            ORDER BY started_at DESC
            LIMIT 1
            """
        )
    )
    return result.first()
