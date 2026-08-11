"""Search, executed.

The generated search_vector, websearch_to_tsquery, the numeric rank and the
trigram fallback all live in the database. None of them can be checked without
one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from api.repositories.search import search, search_fulltext, search_similar
from api.search_cursor import SearchCursor, SearchMode, query_fingerprint

pytestmark = pytest.mark.integration


async def run(url: str, fn: Any, **kwargs: Any) -> Any:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await fn(connection, **kwargs)
    finally:
        await engine.dispose()


class TestFullText:
    async def test_a_title_word_matches(self, seeded: Any, api_database_url: str) -> None:
        await seeded.book("Dune")
        await seeded.book("Neuromancer")

        rows = await run(api_database_url, search_fulltext, query="dune", limit=10)

        assert [row.title for row in rows] == ["Dune"]

    async def test_stemming_works(self, seeded: Any, api_database_url: str) -> None:
        """The 'english' configuration is what makes this more than a LIKE.

        Both words reduce to 'chronicl'. Not every related pair does — 'utopian'
        and 'utopia' are distinct tokens — so the fuzzy fallback matters even
        for correctly spelled queries.
        """
        await seeded.book("The Dune Chronicles")

        rows = await run(api_database_url, search_fulltext, query="chronicle", limit=10)

        assert len(rows) == 1

    async def test_web_search_syntax_is_accepted_not_fatal(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """websearch_to_tsquery rather than to_tsquery.

        to_tsquery raises on input like this, turning a user's stray quote into
        a 500.
        """
        await seeded.book("Dune")

        rows = await run(api_database_url, search_fulltext, query='"dune" -sequel', limit=10)

        assert [row.title for row in rows] == ["Dune"]

    async def test_unparseable_input_does_not_raise(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Dune")

        rows = await run(api_database_url, search_fulltext, query="!!! &&& ((( ", limit=10)

        assert rows == []

    async def test_the_rank_is_numeric_not_float(self, seeded: Any, api_database_url: str) -> None:
        """The contract the whole cursor design rests on.

        A float rank cannot be compared exactly, so a keyset boundary on it
        skips or repeats rows.
        """
        await seeded.book("Dune")

        rows = await run(api_database_url, search_fulltext, query="dune", limit=10)

        assert isinstance(rows[0].rank, Decimal)

    async def test_the_rank_is_rounded_to_the_agreed_precision(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # If the query rounded differently from the cursor, the cursor would
        # name a score no row has.
        await seeded.book("Dune")

        rows = await run(api_database_url, search_fulltext, query="dune", limit=10)

        assert -rows[0].rank.as_tuple().exponent <= 8


class TestFallback:
    async def test_a_misspelling_falls_back_to_similarity(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Neuromancer")

        rows, mode = await run(api_database_url, search, query="neuromancr", limit=10)

        assert mode is SearchMode.SIMILARITY
        assert [row.title for row in rows] == ["Neuromancer"]

    async def test_a_good_query_stays_in_full_text(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Neuromancer")

        _, mode = await run(api_database_url, search, query="neuromancer", limit=10)

        assert mode is SearchMode.FULLTEXT

    async def test_a_thin_full_text_page_does_not_trigger_the_fallback(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # One result is a real answer, not a reason to change rankings
        # halfway through answering.
        await seeded.book("Dune")
        await seeded.book("Neuromancer")

        rows, mode = await run(api_database_url, search, query="dune", limit=10)

        assert mode is SearchMode.FULLTEXT
        assert len(rows) == 1

    async def test_nothing_matches_at_all(self, seeded: Any, api_database_url: str) -> None:
        await seeded.book("Dune")

        rows, _ = await run(api_database_url, search, query="zzzzqqqq", limit=10)

        assert rows == []

    async def test_similarity_scores_are_numeric_too(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Neuromancer")

        rows = await run(api_database_url, search_similar, query="neuromancr", limit=10)

        assert isinstance(rows[0].rank, Decimal)


class TestSearchPagination:
    async def test_paging_returns_each_result_once(
        self, seeded: Any, api_database_url: str
    ) -> None:
        for index in range(7):
            await seeded.book(f"Dune Volume {index}")

        seen: list[int] = []
        cursor: SearchCursor | None = None
        for _ in range(7):
            rows = await run(
                api_database_url, search_fulltext, query="dune", limit=2, cursor=cursor
            )
            page, extra = rows[:2], rows[2:]
            seen.extend(row.id for row in page)
            if not extra:
                break
            last = page[-1]
            cursor = SearchCursor(
                mode=SearchMode.FULLTEXT,
                score=last.rank,
                book_id=last.id,
                query_hash=query_fingerprint("dune"),
            )

        assert len(seen) == 7
        assert len(set(seen)) == 7

    async def test_equal_scores_still_paginate(self, seeded: Any, api_database_url: str) -> None:
        """Identical titles rank identically.

        The id tie-breaker is the only thing separating them; without it a
        cursor on a shared score cannot name a position.
        """
        for _ in range(5):
            await seeded.book("Dune")

        first = await run(api_database_url, search_fulltext, query="dune", limit=2)
        assert first[0].rank == first[1].rank

        cursor = SearchCursor(
            mode=SearchMode.FULLTEXT,
            score=first[1].rank,
            book_id=first[1].id,
            query_hash=query_fingerprint("dune"),
        )
        rest = await run(api_database_url, search_fulltext, query="dune", limit=10, cursor=cursor)

        assert {row.id for row in rest}.isdisjoint({row.id for row in first[:2]})
        assert len(rest) == 3

    async def test_the_over_fetch_signals_more(self, seeded: Any, api_database_url: str) -> None:
        for index in range(4):
            await seeded.book(f"Dune {index}")

        rows = await run(api_database_url, search_fulltext, query="dune", limit=3)

        assert len(rows) == 4


class TestModePinning:
    async def test_a_similarity_cursor_continues_in_similarity(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """A page-two request must continue page one's ranking.

        Re-deciding the mode mid-search interleaves two orderings, and the
        client sees results repeat or vanish across the boundary.
        """
        for index in range(4):
            await seeded.book(f"Neuromancer {index}")

        first, mode = await run(api_database_url, search, query="neuromancr", limit=2)
        assert mode is SearchMode.SIMILARITY

        cursor = SearchCursor(
            mode=SearchMode.SIMILARITY,
            score=first[1].rank,
            book_id=first[1].id,
            query_hash=query_fingerprint("neuromancr"),
        )
        rest, continued = await run(
            api_database_url, search, query="neuromancr", limit=10, cursor=cursor
        )

        assert continued is SearchMode.SIMILARITY
        assert {r.id for r in rest}.isdisjoint({r.id for r in first[:2]})

    async def test_a_fulltext_cursor_continues_in_fulltext(
        self, seeded: Any, api_database_url: str
    ) -> None:
        for index in range(4):
            await seeded.book(f"Dune {index}")

        first, _ = await run(api_database_url, search, query="dune", limit=2)
        cursor = SearchCursor(
            mode=SearchMode.FULLTEXT,
            score=first[1].rank,
            book_id=first[1].id,
            query_hash=query_fingerprint("dune"),
        )

        _, continued = await run(api_database_url, search, query="dune", limit=10, cursor=cursor)

        assert continued is SearchMode.FULLTEXT
