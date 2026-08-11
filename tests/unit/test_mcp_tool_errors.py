"""Tool error paths.

Every one returns text telling the model what to do differently. A tool error
is an instruction, not a log line: "not found" ends the agent's turn, while
"use search_books to find its identifier" continues it.

Exercised directly rather than over the protocol — the happy paths go through a
real MCP session against a real database; these are branches a database cannot
reach.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from api.config import Settings
from api.mcp.tools import CatalogueTools, compact


class FakeEngine:
    """Yields whatever the test set, without a database."""

    def __init__(self, **returns: Any) -> None:
        self.returns = returns

    def connect(self) -> Any:
        outer = self

        class Ctx:
            async def __aenter__(self) -> Any:
                return outer

            async def __aexit__(self, *_exc: object) -> None:
                return None

        return Ctx()


@pytest.fixture
def tools() -> CatalogueTools:
    return CatalogueTools(FakeEngine(), Settings())  # type: ignore[arg-type,call-arg]


class TestCompact:
    def test_it_drops_nulls(self) -> None:
        # "publisher": null tells a model nothing the absent key does not, and
        # costs tokens on every record of every call.
        assert compact({"a": 1, "b": None}) == {"a": 1}

    def test_it_drops_empty_collections(self) -> None:
        assert compact({"a": 1, "b": [], "c": {}}) == {"a": 1}

    def test_it_keeps_falsy_values_that_mean_something(self) -> None:
        # 0 pages and False confirmed are real answers.
        assert compact({"pages": 0, "confirmed": False}) == {"pages": 0, "confirmed": False}


class TestGetBookErrors:
    async def test_neither_argument(self, tools: CatalogueTools) -> None:
        result = await tools.get_book()

        assert "isbn13 or id" in result["error"]
        assert "search_books" in result["error"]

    async def test_a_malformed_isbn_names_the_rule(self, tools: CatalogueTools) -> None:
        result = await tools.get_book(isbn13="123")

        assert "13 digits" in result["error"]

    async def test_a_bad_check_digit_is_refused(self, tools: CatalogueTools) -> None:
        result = await tools.get_book(isbn13="9780553380164")

        assert "check digit" in result["error"]

    async def test_an_unknown_book_points_at_search(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("api.repositories.books.get_book", none)

        result = await tools.get_book(isbn13="9780553380163")

        assert "search_books" in result["error"]

    async def test_an_unknown_id_points_at_search(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("api.repositories.books.get_book_by_id", none)

        result = await tools.get_book(id=999)

        assert "search_books" in result["error"]


class TestGetSeriesErrors:
    async def test_neither_argument(self, tools: CatalogueTools) -> None:
        assert "name or id" in (await tools.get_series())["error"]

    async def test_an_unknown_name_suggests_search(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("api.repositories.series.find_series_by_name", none)

        result = await tools.get_series(name="nonexistent")

        assert "search_books" in result["error"]

    async def test_an_unknown_id_suggests_search(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("api.repositories.series.get_series", none)

        result = await tools.get_series(id=999)

        assert "search_books" in result["error"]


class TestProvenanceErrors:
    async def test_neither_argument(self, tools: CatalogueTools) -> None:
        assert "isbn13 or id" in (await tools.get_book_provenance())["error"]

    async def test_a_malformed_isbn_is_refused(self, tools: CatalogueTools) -> None:
        assert "ISBN-13" in (await tools.get_book_provenance(isbn13="abc"))["error"]

    async def test_an_unknown_book_points_at_search(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def none(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr("api.repositories.books.get_book_by_id", none)

        result = await tools.get_book_provenance(id=999)

        assert "search_books" in result["error"]


class TestSubjectTruncation:
    async def test_a_long_subject_list_is_truncated_with_a_count(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Beyond a dozen, subjects are noise in a context window.

        The remainder count is kept so the model knows the list was cut rather
        than that the book simply has twelve subjects.
        """
        row = SimpleNamespace(
            id=1,
            isbn13="9780553380163",
            title="Dune",
            subtitle=None,
            published_year=1965,
            publisher=None,
            page_count=None,
            language="eng",
            cover_url=None,
            goodreads_average_rating=Decimal("4.25"),
            download_count=None,
        )

        async def one(*_a: Any, **_k: Any) -> Any:
            return row

        async def many_subjects(*_a: Any, **_k: Any) -> list[str]:
            return [f"subject {n}" for n in range(30)]

        async def no_authors(*_a: Any, **_k: Any) -> dict[int, list[Any]]:
            return {}

        async def no_series(*_a: Any, **_k: Any) -> list[Any]:
            return []

        monkeypatch.setattr("api.repositories.books.get_book", one)
        monkeypatch.setattr("api.repositories.books.subjects_for", many_subjects)
        monkeypatch.setattr("api.repositories.books.authors_for", no_authors)
        monkeypatch.setattr("api.repositories.books.series_for", no_series)

        result = await tools.get_book(isbn13="9780553380163")

        assert len(result["subjects"]) == 12
        assert result["subjects_omitted"] == 18

    async def test_a_rating_is_rendered_without_float_noise(
        self, tools: CatalogueTools, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sent as a float, 4.25 can arrive as 4.250000000000001 and a model
        # will repeat that back verbatim.
        row = SimpleNamespace(
            id=1,
            isbn13=None,
            title="Dune",
            subtitle=None,
            published_year=None,
            publisher=None,
            page_count=None,
            language=None,
            cover_url=None,
            goodreads_average_rating=Decimal("4.2500"),
            download_count=None,
        )

        async def one(*_a: Any, **_k: Any) -> Any:
            return row

        async def empty(*_a: Any, **_k: Any) -> Any:
            return []

        async def no_authors(*_a: Any, **_k: Any) -> dict[int, list[Any]]:
            return {}

        monkeypatch.setattr("api.repositories.books.get_book_by_id", one)
        monkeypatch.setattr("api.repositories.books.subjects_for", empty)
        monkeypatch.setattr("api.repositories.books.authors_for", no_authors)
        monkeypatch.setattr("api.repositories.books.series_for", empty)

        result = await tools.get_book(id=1)

        assert result["rating"] == "4.25"
