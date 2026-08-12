"""Books whose sources disagree the most.

A catalogue merged from sources of differing reliability does not have uniform
confidence. Most books are corroborated; a minority are contested, and those
are the ones worth a second look — by a person, or by a source held back for
exactly this purpose.

Finding them is a query rather than a judgement: count the fields on which a
book's sources reported different values, and rank by that count. What to *do*
about a contested book is a separate decision, deliberately kept out of here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

# Only books more than one source actually reported on can be contested; a
# single-source book is unanimous by construction, which is not the same as
# being right.
_CONTESTED = """
SELECT b.id, b.isbn13, b.title, b.published_year,
       count(DISTINCT bs.source) AS sources
FROM books AS b
JOIN book_sources AS bs ON bs.book_id = b.id
GROUP BY b.id, b.isbn13, b.title, b.published_year
HAVING count(DISTINCT bs.source) > 1
ORDER BY count(DISTINCT bs.source) DESC, b.id
LIMIT :limit
"""


async def multi_source_books(connection: AsyncConnection, *, limit: int = 200) -> list[Row[Any]]:
    """Books with more than one source, most-corroborated first.

    Field-level conflict is counted in the application rather than in SQL: the
    comparison has to normalise across each source's own spelling of a field,
    and encoding that in a query would duplicate logic the MCP layer already
    owns and would drift from it.
    """
    result = await connection.execute(text(_CONTESTED), {"limit": limit})
    return list(result)


async def payloads_for(connection: AsyncConnection, book_ids: list[int]) -> dict[int, list[Any]]:
    """Every source payload for a set of books, in one round trip."""
    if not book_ids:
        return {}

    result = await connection.execute(
        text(
            """
            SELECT book_id, source, raw_payload
            FROM book_sources
            WHERE book_id = ANY(:book_ids)
            ORDER BY book_id, source
            """
        ),
        {"book_ids": book_ids},
    )

    grouped: dict[int, list[Any]] = {}
    for row in result:
        grouped.setdefault(row.book_id, []).append(row)
    return grouped
