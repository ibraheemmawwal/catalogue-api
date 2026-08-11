"""Provenance queries.

The reason the MCP surface exists. Every other read here answers "what does the
catalogue say"; this one answers "who said it, and does anything disagree".

A general-purpose book API cannot answer that, because it has one source. This
catalogue merged four of differing reliability — one of them unofficial and
demonstrably wrong about at least one ISBN — so the disagreements are real and
worth surfacing rather than resolving silently.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection


async def sources_for(connection: AsyncConnection, *, book_id: int) -> list[Row[Any]]:
    """Every source that supplied this book, most recently seen first.

    ``raw_payload`` comes back with it: the disagreement check reads the values
    each source actually reported, not the merged result. Comparing merged
    fields would only ever show agreement with itself.
    """
    result = await connection.execute(
        text(
            """
            SELECT source, source_id, first_seen_at, last_seen_at, raw_payload
            FROM book_sources
            WHERE book_id = :book_id
            ORDER BY last_seen_at DESC, source
            """
        ),
        {"book_id": book_id},
    )
    return list(result)
