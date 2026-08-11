"""The MCP tools, over a real client session against a real database.

Driven through the protocol rather than by calling the Python functions. An
agent reaches these through MCP, and mounting, schema generation and
serialisation are precisely what a direct call skips.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from api.config import Settings
from api.main import create_app
from api.mcp import descriptions

pytestmark = pytest.mark.integration


@asynccontextmanager
async def mcp_session(database_url: str) -> AsyncIterator[ClientSession]:
    """A client talking to the app over Streamable HTTP."""
    import uvicorn

    settings = Settings(database_url=database_url.replace("+asyncpg", ""))  # type: ignore[arg-type]
    app = create_app(settings)

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    import asyncio

    task = asyncio.create_task(server.serve())
    # uvicorn exposes readiness as a polled flag rather than an event, so
    # this is the interface it gives us.
    while not server.started:  # noqa: ASYNC110
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        # v2 yields two streams; earlier releases yielded a third handle.
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session
    finally:
        server.should_exit = True
        await task


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    assert result.structured_content is not None, result.content
    return dict(result.structured_content)


class TestDiscovery:
    async def test_all_five_tools_are_offered(self, seeded: Any, api_database_url: str) -> None:
        async with mcp_session(api_database_url) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}

        assert names == {
            "search_books",
            "get_book",
            "get_series",
            "get_book_provenance",
            "catalogue_stats",
        }

    async def test_every_description_states_when_to_call(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The rule the descriptions module exists to enforce.

        A description that only says what a tool returns leaves selection to
        guesswork; stating the trigger condition measurably improves it.
        """
        async with mcp_session(api_database_url) as session:
            tools = (await session.list_tools()).tools

        for tool in tools:
            assert tool.description, tool.name
            assert "Call this" in tool.description, tool.name

    async def test_no_description_shouts(self, seeded: Any, api_database_url: str) -> None:
        # Emphasis written for models that under-triggered now causes
        # over-triggering, which is the quieter failure.
        # Only the exported constants — the module docstring names these
        # patterns in order to explain why they are avoided.
        exported = {
            name: value
            for name, value in vars(descriptions).items()
            if name.isupper() and isinstance(value, str)
        }
        assert exported

        for name, text in exported.items():
            assert "CRITICAL" not in text, name
            assert "YOU MUST" not in text, name


class TestSearchTool:
    async def test_it_finds_a_book_by_title(self, seeded: Any, api_database_url: str) -> None:
        book_id = await seeded.book("Dune", isbn13="9780553380163", year=1965)
        await seeded.author(book_id, "Frank Herbert")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "search_books", query="dune")

        assert result["matches"][0]["title"] == "Dune"
        assert result["matches"][0]["authors"] == ["Frank Herbert"]

    async def test_results_are_compact(self, seeded: Any, api_database_url: str) -> None:
        # A tool result lands in a context window; publisher and page count
        # belong behind get_book, not in every search hit.
        await seeded.book("Dune", isbn13="9780553380163", year=1965)

        async with mcp_session(api_database_url) as session:
            match = (await call(session, "search_books", query="dune"))["matches"][0]

        assert set(match) <= {"id", "isbn13", "title", "authors", "year"}

    async def test_empty_fields_are_omitted_not_null(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Untitled Work", isbn13=None, year=None)

        async with mcp_session(api_database_url) as session:
            match = (await call(session, "search_books", query="untitled"))["matches"][0]

        assert "isbn13" not in match
        assert "year" not in match

    async def test_it_says_when_more_matches_exist(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # Instead of a cursor: an agent threading an opaque token across turns
        # loses its place, whereas "narrow it" is actionable.
        for index in range(8):
            await seeded.book(f"Dune Volume {index}")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "search_books", query="dune", limit=2)

        assert result["more_available"] is True
        assert "Narrow" in result["hint"]

    async def test_the_limit_is_capped(self, seeded: Any, api_database_url: str) -> None:
        for index in range(60):
            await seeded.book(f"Book {index:03d}")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "search_books", limit=500)

        assert result["shown"] <= 50

    async def test_filters_work_without_a_query(self, seeded: Any, api_database_url: str) -> None:
        wanted = await seeded.book("Wanted", year=1965)
        await seeded.author(wanted, "Frank Herbert")
        await seeded.book("Other", year=1965)

        async with mcp_session(api_database_url) as session:
            result = await call(session, "search_books", author="Herbert")

        assert [m["title"] for m in result["matches"]] == ["Wanted"]

    async def test_an_impossible_year_range_explains_itself(
        self, seeded: Any, api_database_url: str
    ) -> None:
        async with mcp_session(api_database_url) as session:
            result = await call(session, "search_books", year_from=2000, year_to=1990)

        assert "year_from" in result["error"]


class TestGetBook:
    async def test_it_returns_the_full_record(self, seeded: Any, api_database_url: str) -> None:
        book_id = await seeded.book("Dune", isbn13="9780553380163", year=1965)
        await seeded.author(book_id, "Frank Herbert")
        await seeded.subject(book_id, "science fiction")

        async with mcp_session(api_database_url) as session:
            book = await call(session, "get_book", isbn13="9780553380163")

        assert book["title"] == "Dune"
        assert book["subjects"] == ["science fiction"]

    async def test_it_accepts_an_id_for_books_without_an_isbn(
        self, seeded: Any, api_database_url: str
    ) -> None:
        book_id = await seeded.book("No ISBN", isbn13=None)

        async with mcp_session(api_database_url) as session:
            book = await call(session, "get_book", id=book_id)

        assert book["title"] == "No ISBN"

    async def test_a_bad_isbn_says_what_to_do(self, seeded: Any, api_database_url: str) -> None:
        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_book", isbn13="123")

        assert "search_books" in result["error"]

    async def test_neither_argument_is_an_actionable_error(
        self, seeded: Any, api_database_url: str
    ) -> None:
        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_book")

        assert "isbn13 or id" in result["error"]


class TestGetSeries:
    async def test_it_finds_a_series_by_name(self, seeded: Any, api_database_url: str) -> None:
        first = await seeded.book("Dune")
        second = await seeded.book("Dune Messiah")
        await seeded.series(first, "Dune Chronicles", position="1", confirmed=True)
        await seeded.series(second, "Dune Chronicles", position="2", confirmed=True)

        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_series", name="dune chronicles")

        assert [b["title"] for b in result["books"]] == ["Dune", "Dune Messiah"]

    async def test_inferred_positions_are_flagged(self, seeded: Any, api_database_url: str) -> None:
        """The claim strength an agent should pass on.

        A reading order built from title-pattern guesses is not the same
        statement as one a source made.
        """
        first = await seeded.book("Book One")
        second = await seeded.book("Book Two")
        await seeded.series(first, "Saga", position="1", confirmed=True)
        await seeded.series(second, "Saga", position="2", confirmed=False)

        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_series", name="saga")

        assert "inferred" in result["note"]
        assert result["books"][1]["confirmed"] is False

    async def test_a_fully_confirmed_series_carries_no_caveat(
        self, seeded: Any, api_database_url: str
    ) -> None:
        book_id = await seeded.book("Only")
        await seeded.series(book_id, "Saga", position="1", confirmed=True)

        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_series", name="saga")

        assert "note" not in result

    async def test_an_unknown_series_suggests_search(
        self, seeded: Any, api_database_url: str
    ) -> None:
        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_series", name="nonexistent")

        assert "search_books" in result["error"]


class TestProvenance:
    async def test_it_lists_the_sources(self, seeded: Any, api_database_url: str) -> None:
        book_id = await seeded.book("Dune", isbn13="9780553380163")
        await seeded.source(book_id, "goodreads")
        await seeded.source(book_id, "openlibrary")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_book_provenance", isbn13="9780553380163")

        assert {s["source"] for s in result["sources"]} == {"goodreads", "openlibrary"}

    async def test_agreeing_sources_report_no_disagreement(
        self, seeded: Any, api_database_url: str
    ) -> None:
        book_id = await seeded.book("Dune", isbn13="9780553380163")
        await seeded.source(book_id, "goodreads")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_book_provenance", isbn13="9780553380163")

        assert result["disagreements"] == []

    async def test_a_book_with_no_provenance_is_not_an_error(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Orphan", isbn13="9780441172719")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "get_book_provenance", isbn13="9780441172719")

        assert result["sources"] == []


class TestStats:
    async def test_it_reports_coverage(self, seeded: Any, api_database_url: str) -> None:
        await seeded.book("With year", year=1965)
        await seeded.book("Without year", year=None)

        async with mcp_session(api_database_url) as session:
            result = await call(session, "catalogue_stats")

        assert result["books"] == 2
        assert result["coverage_percent"]["published_year"] == 50.0

    async def test_an_empty_catalogue_does_not_divide_by_zero(
        self, seeded: Any, api_database_url: str
    ) -> None:
        async with mcp_session(api_database_url) as session:
            result = await call(session, "catalogue_stats")

        assert result["books"] == 0
        assert result["coverage_percent"]["isbn13"] == 0.0

    async def test_it_warns_that_absence_is_not_zero(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # The interpretation error this whole tool exists to prevent.
        await seeded.book("Dune")

        async with mcp_session(api_database_url) as session:
            result = await call(session, "catalogue_stats")

        assert "not that the value is zero" in result["note"]


class TestNoDrift:
    async def test_rest_and_mcp_return_the_same_book(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The test that justifies the shared repository layer.

        Two surfaces over one database are only worth having if they cannot
        disagree.
        """
        import httpx

        book_id = await seeded.book("Dune", isbn13="9780553380163", year=1965)
        await seeded.author(book_id, "Frank Herbert")

        settings = Settings(database_url=api_database_url.replace("+asyncpg", ""))  # type: ignore[arg-type]
        app = create_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            rest = (await client.get("/v1/books/9780553380163")).json()

        async with mcp_session(api_database_url) as session:
            mcp = await call(session, "get_book", isbn13="9780553380163")

        assert rest["title"] == mcp["title"]
        assert rest["id"] == mcp["id"]
        assert [a["name"] for a in rest["authors"]] == mcp["authors"]
