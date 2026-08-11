"""The SQL the repository builds.

These assert the claims the comments make — semi-joins rather than joins,
conditional predicates rather than IS NULL guards, row-value cursor comparison.
Each is a correctness or performance decision that a later edit could quietly
undo.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from api.pagination import Cursor
from api.repositories.books import (
    BookFilters,
    authors_for,
    get_book,
    get_book_by_id,
    list_books,
    series_for,
    subjects_for,
    to_book,
    to_summary,
)
from api.schemas.books import AuthorRef, SeriesRef


class CapturingConnection:
    """Records the statement instead of running it."""

    def __init__(self) -> None:
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql = " ".join(str(statement).split())
        self.params = params or {}
        return []


class TestFilterClauses:
    def test_no_filters_produce_no_predicates(self) -> None:
        clauses, params = BookFilters().clauses()

        assert clauses == []
        assert params == {}

    def test_only_supplied_filters_appear(self) -> None:
        """The IS NULL guard this avoids defeats the index.

        `:value IS NULL OR column = :value` reads well and is one statement,
        but the planner cannot know the parameter is null, so it plans for the
        general case and scans.
        """
        clauses, params = BookFilters(language="eng").clauses()

        assert len(clauses) == 1
        assert "language" in params
        assert "year_from" not in params

    def test_relationship_filters_use_semi_joins(self) -> None:
        # A book with three matching subjects must come back once. A JOIN
        # returns it three times and quietly shortens the page.
        clauses, _ = BookFilters(subject="fiction").clauses()

        assert "EXISTS" in clauses[0]
        assert "JOIN subjects" in clauses[0]

    def test_author_and_series_match_by_trigram(self) -> None:
        author_clause, _ = BookFilters(author="herbert").clauses()
        series_clause, _ = BookFilters(series="dune").clauses()

        assert "%" in author_clause[0]
        assert "%" in series_clause[0]

    def test_year_bounds_apply_directly_to_books(self) -> None:
        clauses, params = BookFilters(year_from=1960, year_to=1970).clauses()

        assert len(clauses) == 2
        assert params == {"year_from": 1960, "year_to": 1970}


class TestListQuery:
    async def test_it_over_fetches_by_one(self) -> None:
        # The extra row is how "is there more" is answered without a second
        # COUNT over a predicate the planner has already walked.
        connection = CapturingConnection()

        await list_books(connection, filters=BookFilters(), limit=20)  # type: ignore[arg-type]

        assert connection.params["limit_plus_one"] == 21

    async def test_it_orders_by_the_keyset(self) -> None:
        connection = CapturingConnection()

        await list_books(connection, filters=BookFilters(), limit=5)  # type: ignore[arg-type]

        assert "ORDER BY lower(b.title), b.id" in connection.sql

    async def test_no_filters_means_no_where_clause(self) -> None:
        connection = CapturingConnection()

        await list_books(connection, filters=BookFilters(), limit=5)  # type: ignore[arg-type]

        assert "WHERE" not in connection.sql

    async def test_a_cursor_uses_row_value_comparison(self) -> None:
        """Matching the (lower(title), id) index exactly.

        Spelled as an OR of two comparisons it returns the same rows and loses
        the index — the kind of rewrite that looks equivalent in review.
        """
        connection = CapturingConnection()
        cursor = Cursor(sort_title="dune", book_id=uuid4())

        await list_books(connection, filters=BookFilters(), limit=5, cursor=cursor)  # type: ignore[arg-type]

        assert "(lower(b.title), b.id) > (:cursor_title, :cursor_id)" in connection.sql

    async def test_a_cursor_combines_with_filters(self) -> None:
        connection = CapturingConnection()

        await list_books(  # type: ignore[arg-type]
            connection,
            filters=BookFilters(language="eng"),
            limit=5,
            cursor=Cursor(sort_title="dune", book_id=uuid4()),
        )

        assert "b.language = :language" in connection.sql
        assert "cursor_title" in connection.params

    async def test_the_collection_query_does_not_join_relationships(self) -> None:
        # Joining authors here multiplies rows before LIMIT and returns fewer
        # books than asked for — only for books with several authors, which is
        # why it survives casual testing.
        connection = CapturingConnection()

        await list_books(connection, filters=BookFilters(subject="fiction"), limit=5)  # type: ignore[arg-type]

        body = connection.sql.split("EXISTS")[0]
        assert "JOIN" not in body


class ResultConnection:
    """Returns fixed rows, and records what was asked."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> Any:
        self.sql = " ".join(str(statement).split())
        self.params = params or {}
        return _Rows(self._rows)


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def __iter__(self) -> Any:
        return iter(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class TestRelationshipLoading:
    async def test_authors_are_grouped_by_book(self) -> None:
        first, second = uuid4(), uuid4()
        author_a, author_b = uuid4(), uuid4()
        connection = ResultConnection(
            [(first, author_a, "Frank Herbert"), (second, author_b, "Ursula Le Guin")]
        )

        grouped = await authors_for(connection, [first, second])  # type: ignore[arg-type]

        assert grouped[first][0].name == "Frank Herbert"
        assert grouped[second][0].name == "Ursula Le Guin"

    async def test_several_authors_on_one_book_stay_together(self) -> None:
        book = uuid4()
        connection = ResultConnection([(book, uuid4(), "A"), (book, uuid4(), "B")])

        grouped = await authors_for(connection, [book])  # type: ignore[arg-type]

        assert [a.name for a in grouped[book]] == ["A", "B"]

    async def test_an_empty_page_asks_nothing(self) -> None:
        # A page with no rows should not produce a query with an empty ANY().
        connection = ResultConnection([])

        assert await authors_for(connection, []) == {}  # type: ignore[arg-type]
        assert connection.sql == ""

    async def test_one_query_serves_a_whole_page(self) -> None:
        connection = ResultConnection([])
        ids = [uuid4() for _ in range(20)]

        await authors_for(connection, ids)  # type: ignore[arg-type]

        assert "ANY(:book_ids)" in connection.sql
        assert len(connection.params["book_ids"]) == 20

    async def test_subjects_come_back_ordered(self) -> None:
        connection = ResultConnection([("fiction",), ("science fiction",)])

        assert await subjects_for(connection, uuid4()) == ["fiction", "science fiction"]  # type: ignore[arg-type]

    async def test_series_membership_keeps_position_and_confirmation(self) -> None:
        # Both are load-bearing: a position inferred from a title pattern is a
        # weaker claim than one a source stated.
        series_id = uuid4()
        connection = ResultConnection([(series_id, "Dune", Decimal("4.5"), False)])

        members = await series_for(connection, uuid4())  # type: ignore[arg-type]

        assert members[0].position == Decimal("4.5")
        assert members[0].confirmed is False


class TestSingleBookLookup:
    async def test_a_known_isbn_returns_a_row(self) -> None:
        connection = ResultConnection([("row",)])

        assert await get_book(connection, isbn13="9780553380163") is not None  # type: ignore[arg-type]

    async def test_an_unknown_isbn_returns_none(self) -> None:
        assert await get_book(ResultConnection([]), isbn13="9780553380163") is None  # type: ignore[arg-type]

    async def test_lookup_by_id_exists_for_books_without_an_isbn(self) -> None:
        # Search and the MCP tools hand back IDs; a fallback identity is still
        # an identity.
        connection = ResultConnection([("row",)])

        assert await get_book_by_id(connection, book_id=uuid4()) is not None  # type: ignore[arg-type]
        assert "b.id = :book_id" in connection.sql


class TestRowMapping:
    def _row(self, **overrides: Any) -> Any:
        base = {
            "id": uuid4(),
            "isbn13": "9780553380163",
            "title": "Dune",
            "subtitle": None,
            "published_year": 1965,
            "publisher": "Ace",
            "page_count": 412,
            "language": "eng",
            "cover_url": None,
            "goodreads_average_rating": None,
            "download_count": None,
        }
        return SimpleNamespace(**{**base, **overrides})

    def test_a_summary_flattens_authors_to_names(self) -> None:
        summary = to_summary(self._row(), [AuthorRef(id=uuid4(), name="Frank Herbert")])

        assert summary.authors == ["Frank Herbert"]

    def test_a_book_without_an_isbn_still_maps(self) -> None:
        # Fallback identity: the pipeline loads books that never had an ISBN.
        assert to_summary(self._row(isbn13=None), []).isbn13 is None

    def test_detail_carries_every_relationship(self) -> None:
        book = to_book(
            self._row(),
            authors=[AuthorRef(id=uuid4(), name="Frank Herbert")],
            subjects=["science fiction"],
            series=[SeriesRef(id=uuid4(), name="Dune", position=None, confirmed=True)],
        )

        assert book.authors[0].name == "Frank Herbert"
        assert book.subjects == ["science fiction"]
        assert book.series[0].confirmed is True
