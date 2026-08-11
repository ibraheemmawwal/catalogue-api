"""The search endpoint's own logic.

The SQL is exercised against real PostgreSQL. What lives here is everything
between the request and the query: trimming, cursor handling, and the mode the
response reports.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from api.config import Settings
from api.deps import get_connection
from api.main import create_app
from api.schemas.books import AuthorRef
from api.search_cursor import SearchCursor, SearchMode, query_fingerprint


class FakeRow:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def result_row(title: str = "Dune", identifier: int = 1) -> FakeRow:
    return FakeRow(
        id=identifier,
        isbn13="9780553380163",
        title=title,
        published_year=1965,
        language="eng",
        rank=Decimal("0.12345678"),
    )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    app = create_app(Settings())  # type: ignore[call-arg]

    async def fake_connection() -> Any:
        yield object()

    app.dependency_overrides[get_connection] = fake_connection

    state: dict[str, Any] = {"rows": [], "mode": SearchMode.FULLTEXT, "seen": {}}

    async def fake_search(_conn: Any, **kwargs: Any) -> tuple[list[Any], SearchMode]:
        state["seen"] = kwargs
        return state["rows"], state["mode"]

    async def fake_authors(_conn: Any, book_ids: Any) -> dict[int, list[AuthorRef]]:
        return {bid: [] for bid in book_ids}

    monkeypatch.setattr("api.repositories.search.search", fake_search)
    monkeypatch.setattr("api.repositories.books.authors_for", fake_authors)

    app.state.fake = state
    return app


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


class TestQuerying:
    async def test_results_are_returned_as_summaries(self, api: Any) -> None:
        api.state.fake["rows"] = [result_row()]

        async with client_for(api) as client:
            body = (await client.get("/v1/books/search", params={"q": "dune"})).json()

        assert body["items"][0]["title"] == "Dune"
        assert body["mode"] == "fulltext"

    async def test_the_fallback_mode_is_reported(self, api: Any) -> None:
        # A caller seeing fuzzy results should be able to tell why.
        api.state.fake["rows"] = [result_row()]
        api.state.fake["mode"] = SearchMode.SIMILARITY

        async with client_for(api) as client:
            body = (await client.get("/v1/books/search", params={"q": "dnue"})).json()

        assert body["mode"] == "similarity"

    async def test_no_matches_is_an_empty_page_not_an_error(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books/search", params={"q": "zzzz"})

        assert response.status_code == 200
        assert response.json()["items"] == []

    async def test_the_query_is_trimmed_before_searching(self, api: Any) -> None:
        async with client_for(api) as client:
            await client.get("/v1/books/search", params={"q": "  dune  "})

        assert api.state.fake["seen"]["query"] == "dune"

    async def test_a_full_page_offers_a_cursor(self, api: Any) -> None:
        api.state.fake["rows"] = [result_row(identifier=n) for n in range(3)]

        async with client_for(api) as client:
            body = (await client.get("/v1/books/search", params={"q": "dune", "limit": 2})).json()

        assert len(body["items"]) == 2
        assert body["next_cursor"]


class TestValidation:
    async def test_a_missing_query_is_rejected(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books/search")

        assert response.status_code == 422

    async def test_a_one_character_query_is_rejected(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books/search", params={"q": "a"})

        assert response.status_code == 422

    async def test_whitespace_padding_does_not_smuggle_a_short_query(self, api: Any) -> None:
        """Length is validated before trimming.

        So "  a  " passes the parameter check and arrives as one character.
        """
        async with client_for(api) as client:
            response = await client.get("/v1/books/search", params={"q": "  a  "})

        assert response.status_code == 400
        assert "trimming" in response.json()["detail"]

    async def test_an_unreadable_cursor_is_refused(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get(
                "/v1/books/search", params={"q": "dune", "cursor": "garbage"}
            )

        assert response.status_code == 400

    async def test_a_cursor_from_another_query_is_refused(self, api: Any) -> None:
        # Its scores rank a different result set.
        cursor = SearchCursor(
            mode=SearchMode.FULLTEXT,
            score=Decimal("0.5"),
            book_id=1,
            query_hash=query_fingerprint("foundation"),
        ).encode()

        async with client_for(api) as client:
            response = await client.get("/v1/books/search", params={"q": "dune", "cursor": cursor})

        assert response.status_code == 400
        assert "different query" in response.json()["detail"]

    async def test_a_valid_cursor_pins_the_mode(self, api: Any) -> None:
        cursor = SearchCursor(
            mode=SearchMode.SIMILARITY,
            score=Decimal("0.5"),
            book_id=1,
            query_hash=query_fingerprint("dune"),
        ).encode()

        async with client_for(api) as client:
            response = await client.get("/v1/books/search", params={"q": "dune", "cursor": cursor})

        assert response.status_code == 200
        assert api.state.fake["seen"]["cursor"].mode is SearchMode.SIMILARITY


class TestRouteOrdering:
    async def test_search_is_not_captured_by_the_isbn_route(self, api: Any) -> None:
        """Registration order is load-bearing.

        With /{isbn13} first, every search is rejected as a malformed ISBN.
        """
        async with client_for(api) as client:
            response = await client.get("/v1/books/search", params={"q": "dune"})

        assert response.status_code == 200
        assert "ISBN" not in response.text
