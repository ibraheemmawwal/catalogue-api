"""Book endpoints, driven through the real ASGI stack.

Handlers are exercised through HTTP rather than called directly: routing,
validation, dependency wiring and problem rendering are exactly what a direct
call skips, and they are where this layer breaks.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from api.config import Settings
from api.deps import get_connection
from api.main import create_app
from api.pagination import Cursor
from api.schemas.books import AuthorRef


class FakeRow:
    """A stand-in for a SQLAlchemy Row with attribute access."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def book_row(title: str = "Dune", **overrides: Any) -> FakeRow:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "isbn13": "9780553380163",
        "title": title,
        "subtitle": None,
        "published_year": 1965,
        "publisher": "Ace",
        "page_count": 412,
        "language": "eng",
        "cover_url": None,
        "goodreads_average_rating": None,
        "download_count": None,
        "sort_title": title.lower(),
    }
    return FakeRow(**{**defaults, **overrides})


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    """An app whose repository calls are stubbed, but whose stack is real."""
    app = create_app(Settings())  # type: ignore[call-arg]

    async def fake_connection() -> Any:
        yield object()

    app.dependency_overrides[get_connection] = fake_connection

    state: dict[str, Any] = {"rows": [], "authors": {}, "subjects": [], "series": []}

    async def fake_list(_conn: Any, **_kwargs: Any) -> list[Any]:
        return state["rows"]

    async def fake_authors(_conn: Any, book_ids: Any) -> dict[UUID, list[AuthorRef]]:
        return {bid: state["authors"].get(bid, []) for bid in book_ids}

    async def fake_get(_conn: Any, *, isbn13: str) -> Any:
        return next((r for r in state["rows"] if r.isbn13 == isbn13), None)

    async def fake_subjects(_conn: Any, _book_id: UUID) -> list[str]:
        return state["subjects"]

    async def fake_series(_conn: Any, _book_id: UUID) -> list[Any]:
        return state["series"]

    monkeypatch.setattr("api.repositories.books.list_books", fake_list)
    monkeypatch.setattr("api.repositories.books.authors_for", fake_authors)
    monkeypatch.setattr("api.repositories.books.get_book", fake_get)
    monkeypatch.setattr("api.repositories.books.subjects_for", fake_subjects)
    monkeypatch.setattr("api.repositories.books.series_for", fake_series)

    app.state.fake = state
    return app


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


class TestListing:
    async def test_an_empty_catalogue_returns_an_empty_page(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books")

        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}

    async def test_books_are_returned_as_summaries(self, api: Any) -> None:
        row = book_row()
        api.state.fake["rows"] = [row]
        api.state.fake["authors"] = {row.id: [AuthorRef(id=uuid4(), name="Frank Herbert")]}

        async with client_for(api) as client:
            body = (await client.get("/v1/books")).json()

        assert body["items"][0]["title"] == "Dune"
        assert body["items"][0]["authors"] == ["Frank Herbert"]

    async def test_a_summary_omits_detail_fields(self, api: Any) -> None:
        # The split is the point: a list response should not carry publisher,
        # page count and subject arrays for a view that renders none of them.
        api.state.fake["rows"] = [book_row()]

        async with client_for(api) as client:
            item = (await client.get("/v1/books")).json()["items"][0]

        assert "publisher" not in item
        assert "subjects" not in item

    async def test_a_full_page_offers_a_cursor(self, api: Any) -> None:
        api.state.fake["rows"] = [book_row(f"Book {n}") for n in range(3)]

        async with client_for(api) as client:
            body = (await client.get("/v1/books", params={"limit": 2})).json()

        assert len(body["items"]) == 2
        assert body["next_cursor"]

    async def test_the_last_page_offers_none(self, api: Any) -> None:
        api.state.fake["rows"] = [book_row("Only")]

        async with client_for(api) as client:
            body = (await client.get("/v1/books", params={"limit": 20})).json()

        assert body["next_cursor"] is None


class TestListingValidation:
    async def test_an_impossible_year_range_is_refused(self, api: Any) -> None:
        """An empty page here would read as 'no such books'.

        That sends the caller looking for a data problem that does not exist.
        """
        async with client_for(api) as client:
            response = await client.get("/v1/books", params={"year_from": 2000, "year_to": 1990})

        assert response.status_code == 400
        assert "year_from" in response.json()["detail"]

    async def test_an_unreadable_cursor_says_how_to_recover(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books", params={"cursor": "garbage!!"})

        assert response.status_code == 400
        assert "first page" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("param", "value"),
        [("limit", 0), ("limit", 500), ("language", "english"), ("year_from", 1200)],
    )
    async def test_out_of_range_parameters_are_named(
        self, api: Any, param: str, value: Any
    ) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books", params={param: value})

        assert response.status_code == 422
        assert param in response.json()["detail"]

    async def test_a_valid_cursor_is_accepted(self, api: Any) -> None:
        api.state.fake["rows"] = [book_row()]
        cursor = Cursor(sort_title="a", book_id=uuid4()).encode()

        async with client_for(api) as client:
            response = await client.get("/v1/books", params={"cursor": cursor})

        assert response.status_code == 200


class TestDetail:
    async def test_a_known_isbn_returns_the_full_record(self, api: Any) -> None:
        api.state.fake["rows"] = [book_row()]
        api.state.fake["subjects"] = ["science fiction"]

        async with client_for(api) as client:
            body = (await client.get("/v1/books/9780553380163")).json()

        assert body["title"] == "Dune"
        assert body["publisher"] == "Ace"
        assert body["subjects"] == ["science fiction"]

    async def test_jacket_formatting_is_accepted(self, api: Any) -> None:
        api.state.fake["rows"] = [book_row()]

        async with client_for(api) as client:
            response = await client.get("/v1/books/978-0-553-38016-3")

        assert response.status_code == 200

    async def test_a_malformed_isbn_explains_the_format(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books/12345")

        assert response.status_code == 400
        assert "13 digits" in response.json()["detail"]

    async def test_a_bad_check_digit_is_a_400_not_a_404(self, api: Any) -> None:
        # The distinction matters: 404 means "we don't have it", 400 means
        # "you mistyped it". Only one of those is worth searching for.
        async with client_for(api) as client:
            response = await client.get("/v1/books/9780553380164")

        assert response.status_code == 400

    async def test_an_unknown_isbn_points_at_search(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/books/9780441172719")

        assert response.status_code == 404
        assert "search" in response.json()["detail"].lower()
