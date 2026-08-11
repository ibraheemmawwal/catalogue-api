"""Full-text search, with a trigram fallback.

Two modes, tried in order. Full text answers real queries well and returns
nothing at all for a misspelling; trigram similarity catches the misspelling
and ranks poorly on a good query. Running full text first and falling back only
on an empty first page gets both without paying for both.

The scores are `numeric`, rounded before ordering, and never pass through a
float. ``ts_rank`` returns ``real``: convert it late, or compare it as a float,
and the value shifts in its last bits — which for keyset pagination means a
cursor naming a score no row has, skipping or repeating rows at every boundary.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Numeric, Row, bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from api.search_cursor import SCORE_PRECISION, SearchCursor, SearchMode

_SELECTED = """
    b.id, b.isbn13, b.title, b.published_year, b.language
"""

# websearch_to_tsquery parses ordinary search syntax (quotes, OR, -exclusion)
# and, unlike to_tsquery, cannot be made to raise on user input.
_FULLTEXT = """
WITH query_input AS (
    SELECT websearch_to_tsquery('english', :query) AS tsq
),
ranked AS (
    SELECT b.id,
           round(ts_rank(b.search_vector, q.tsq)::numeric, {precision}) AS rank
    FROM books AS b
    CROSS JOIN query_input AS q
    WHERE b.search_vector @@ q.tsq
    {cursor_clause}
)
SELECT {selected}, ranked.rank
FROM ranked
JOIN books AS b USING (id)
ORDER BY ranked.rank DESC, b.id DESC
LIMIT :limit_plus_one
"""

# The fallback. series_search_text is what lets "dune chronicles" find member
# books whose own titles never mention the series.
_SIMILARITY = """
WITH ranked AS (
    SELECT b.id,
           round(
               greatest(
                   similarity(b.title, :query),
                   similarity(coalesce(b.series_search_text, ''), :query)
               )::numeric,
               {precision}
           ) AS rank
    FROM books AS b
    WHERE b.title %% :query OR coalesce(b.series_search_text, '') %% :query
),
filtered AS (
    SELECT * FROM ranked WHERE rank > 0 {cursor_clause}
)
SELECT {selected}, filtered.rank
FROM filtered
JOIN books AS b USING (id)
ORDER BY filtered.rank DESC, b.id DESC
LIMIT :limit_plus_one
"""

_CURSOR_CLAUSE = "AND (rank, id) < (:cursor_score, :cursor_id)"
_FULLTEXT_CURSOR_CLAUSE = (
    "AND (round(ts_rank(b.search_vector, q.tsq)::numeric, {precision}), b.id) "
    "< (:cursor_score, :cursor_id)"
)


def _statement(sql: str) -> Any:
    """Bind the score as NUMERIC so the comparison stays exact.

    Without the explicit type asyncpg infers the parameter, and an inferred
    float8 silently reintroduces the imprecision the rounding removed.
    """
    return text(sql).bindparams(bindparam("cursor_score", type_=Numeric(38, SCORE_PRECISION)))


async def search_fulltext(
    connection: AsyncConnection,
    *,
    query: str,
    limit: int,
    cursor: SearchCursor | None = None,
) -> list[Row[Any]]:
    params: dict[str, Any] = {"query": query, "limit_plus_one": limit + 1}
    clause = ""
    if cursor is not None:
        clause = _FULLTEXT_CURSOR_CLAUSE.format(precision=SCORE_PRECISION)
        params["cursor_score"] = cursor.score
        params["cursor_id"] = cursor.book_id

    sql = _FULLTEXT.format(precision=SCORE_PRECISION, cursor_clause=clause, selected=_SELECTED)
    statement = _statement(sql) if cursor is not None else text(sql)
    return list(await connection.execute(statement, params))


async def search_similar(
    connection: AsyncConnection,
    *,
    query: str,
    limit: int,
    cursor: SearchCursor | None = None,
) -> list[Row[Any]]:
    params: dict[str, Any] = {"query": query, "limit_plus_one": limit + 1}
    clause = ""
    if cursor is not None:
        clause = _CURSOR_CLAUSE
        params["cursor_score"] = cursor.score
        params["cursor_id"] = cursor.book_id

    sql = _SIMILARITY.format(
        precision=SCORE_PRECISION, cursor_clause=clause, selected=_SELECTED
    ).replace("%%", "%")
    statement = _statement(sql) if cursor is not None else text(sql)
    return list(await connection.execute(statement, params))


async def search(
    connection: AsyncConnection,
    *,
    query: str,
    limit: int,
    cursor: SearchCursor | None = None,
) -> tuple[list[Row[Any]], SearchMode]:
    """Search, choosing the mode.

    A cursor pins the mode: a page-two request must continue the ranking
    page one produced, not re-decide it and interleave two orderings.
    """
    if cursor is not None:
        if cursor.mode is SearchMode.SIMILARITY:
            return await search_similar(connection, query=query, limit=limit, cursor=cursor), (
                SearchMode.SIMILARITY
            )
        return await search_fulltext(connection, query=query, limit=limit, cursor=cursor), (
            SearchMode.FULLTEXT
        )

    rows = await search_fulltext(connection, query=query, limit=limit)
    if rows:
        return rows, SearchMode.FULLTEXT

    # Only when full text found nothing at all — a thin first page is a real
    # result, not a reason to switch rankings mid-answer.
    return await search_similar(connection, query=query, limit=limit), SearchMode.SIMILARITY
