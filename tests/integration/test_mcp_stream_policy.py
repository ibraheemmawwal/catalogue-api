"""Refusing the SSE stream, and the client working anyway.

The refusal is only correct if it is invisible to a real client. The MCP spec
requires a server without a server-initiated stream to answer GET with 405 and
requires clients to carry on over POST — but a spec is a claim about clients,
and the client that matters is the one in front of this service.

So the test that decides this is not the one asserting 405. It is the one that
opens a genuine session against the refusing server and calls a tool.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from api.config import Settings
from api.main import create_app

pytestmark = pytest.mark.integration


def app_for(database_url: str, *, offer_stream: bool) -> Any:
    return create_app(
        Settings(  # type: ignore[call-arg]
            database_url=database_url.replace("+asyncpg", ""),
            mcp_offer_server_stream=offer_stream,
        )
    )


@asynccontextmanager
async def serving(app: Any) -> AsyncIterator[str]:
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    finally:
        server.should_exit = True
        await task


class TestTheClientStillWorks:
    async def test_a_real_session_completes_against_a_refusing_server(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The test this change lives or dies by.

        Everything else here asserts that a rule was applied. This asserts the
        rule did not break the product: a genuine client, over the genuine
        transport, initialising and calling a tool with the stream refused.
        """
        await seeded.book("Dune", isbn13="9780553380163", year=1965)
        app = app_for(api_database_url, offer_stream=False)

        async with (
            serving(app) as base,
            streamable_http_client(f"{base}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            result = await session.call_tool("catalogue_stats", {})

        assert "catalogue_stats" in tools
        assert result.structured_content is not None
        assert result.is_error is False

    async def test_tool_calls_still_go_over_post(self, seeded: Any, api_database_url: str) -> None:
        # POST is where every client-to-server message already went; the
        # refusal must not have touched it.
        app = app_for(api_database_url, offer_stream=False)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        async with serving(app) as base, httpx.AsyncClient() as client:
            response = await client.post(f"{base}/mcp", json={}, headers=headers)

        assert response.status_code != 405


class TestTheRefusal:
    async def test_a_stream_request_is_refused(self, seeded: Any, api_database_url: str) -> None:
        app = app_for(api_database_url, offer_stream=False)
        headers = {"Accept": "text/event-stream"}

        async with serving(app) as base, httpx.AsyncClient() as client:
            response = await client.get(f"{base}/mcp", headers=headers)

        assert response.status_code == 405

    async def test_it_says_which_methods_remain(self, seeded: Any, api_database_url: str) -> None:
        """``Allow`` is required on a 405, and is the useful half of the answer.

        A client reading it learns the endpoint is alive and which verb to use,
        rather than concluding MCP is unavailable here.
        """
        app = app_for(api_database_url, offer_stream=False)

        async with serving(app) as base, httpx.AsyncClient() as client:
            response = await client.get(f"{base}/mcp", headers={"Accept": "text/event-stream"})

        assert response.headers["allow"] == "POST, DELETE"
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["status"] == 405

    async def test_it_returns_at_once_rather_than_holding_the_connection(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The entire point, stated as the thing that costs money.

        The stream was billed for its duration — 61 seconds a connection. A
        refusal that took the same 61 seconds to arrive would change nothing,
        so what matters is not the status code but that the request ends.
        """
        app = app_for(api_database_url, offer_stream=False)

        async with serving(app) as base, httpx.AsyncClient() as client:
            started = asyncio.get_running_loop().time()
            await client.get(f"{base}/mcp", headers={"Accept": "text/event-stream"})
            elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 1.0, f"the refusal took {elapsed:.1f}s; it must not hold the connection"


class TestTheRedirectIsCollapsed:
    """One request per call, not two.

    The sub-app is mounted at /mcp and serves its own root, so Starlette
    answered a bare /mcp with a 307 to /mcp/ and clients followed it. Harmless
    until something counted requests: with the rate limiter on, a burst of 25
    became twelve calls and the live smoke test failed on its second MCP
    session.
    """

    async def test_a_bare_path_is_served_rather_than_redirected(
        self, seeded: Any, api_database_url: str
    ) -> None:
        app = app_for(api_database_url, offer_stream=False)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        async with serving(app) as base, httpx.AsyncClient() as client:
            # No follow_redirects: a 307 here is the bug.
            response = await client.post(f"{base}/mcp", json={}, headers=headers)

        assert response.status_code != 307, "the redirect is back; every call now costs two"

    async def test_a_real_session_still_reaches_it(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # Rewriting a path under the router is the kind of change that works
        # against a raw POST and breaks the client that matters.
        await seeded.book("Dune", isbn13="9780553380163", year=1965)
        app = app_for(api_database_url, offer_stream=False)

        async with (
            serving(app) as base,
            streamable_http_client(f"{base}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("catalogue_stats", {})

        assert result.is_error is False


class TestNothingIsHeldOpen:
    """The half the 405 missed.

    Refusing GET moved the cost rather than removing it: with the stream
    refused, a real client held a *POST* open for the same 61.000 seconds,
    because in Streamable HTTP a POST may answer with an SSE stream too.
    Measured on the deployed service, on the first reconnect after the 405
    shipped.

    ``json_response=True`` answers each POST with one JSON body and closes.
    Every tool here is request-response, so a stream carried one message and
    then waited — and waiting is the part Cloud Run bills.
    """

    async def test_a_tool_call_returns_json_rather_than_a_stream(
        self, seeded: Any, api_database_url: str
    ) -> None:
        await seeded.book("Dune", isbn13="9780553380163", year=1965)
        app = app_for(api_database_url, offer_stream=False)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        initialise = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }

        async with serving(app) as base, httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{base}/mcp", json=initialise, headers=headers)

        assert response.status_code == 200
        # text/event-stream here is the 61-second hold coming back.
        assert response.headers["content-type"].startswith("application/json"), (
            "the transport answered with a stream; a POST can be held open too"
        )

    async def test_no_request_is_held_open(self, seeded: Any, api_database_url: str) -> None:
        """The property, stated as time rather than as a header.

        A content-type assertion can pass while something else waits. What
        costs money is a request that does not end, so this measures the thing
        that was actually billed: a full session, wall-clock.
        """
        await seeded.book("Dune", isbn13="9780553380163", year=1965)
        app = app_for(api_database_url, offer_stream=False)

        started = asyncio.get_running_loop().time()
        async with (
            serving(app) as base,
            streamable_http_client(f"{base}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("catalogue_stats", {})
        elapsed = asyncio.get_running_loop().time() - started

        assert result.is_error is False
        # The old behaviour was 61.000s for a single held connection.
        assert elapsed < 15.0, f"a full session took {elapsed:.1f}s; something is being held"


class TestItRefusesOnlyThis:
    async def test_the_rest_api_is_untouched(self, seeded: Any, api_database_url: str) -> None:
        app = app_for(api_database_url, offer_stream=False)

        async with serving(app) as base, httpx.AsyncClient() as client:
            books = await client.get(f"{base}/v1/books")
            live = await client.get(f"{base}/live")

        assert books.status_code == 200
        assert live.status_code == 200

    async def test_turning_it_back_on_restores_the_stream(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The setting has to actually work.

        A server that gains anything server-initiated must be able to offer the
        stream again, and a flag that quietly did nothing would be discovered
        only by the notifications never arriving.

        Against ``/mcp/`` with the trailing slash, because ``/mcp`` is a mount
        redirect — which is also why production logged 2,996 hits on the
        slashed form and one on the bare one.
        """
        app = app_for(api_database_url, offer_stream=True)

        # Streamed and closed on the headers rather than read to completion:
        # this stream never ends, and an earlier version of this test proved
        # it by letting the read time out and walking away. That left the
        # connection open across the server shutdown, and the next thirty-nine
        # tests in the suite failed on a socket this one abandoned.
        async with (
            serving(app) as base,
            httpx.AsyncClient(timeout=5.0) as client,
            client.stream(
                "GET", f"{base}/mcp/", headers={"Accept": "text/event-stream"}
            ) as response,
        ):
            status = response.status_code
            content_type = response.headers.get("content-type", "")

        assert status != 405, "the flag did not restore the stream"
        assert content_type.startswith("text/event-stream")

    async def test_the_slashed_form_is_refused_too(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The form the client actually uses.

        ``/mcp`` redirects to ``/mcp/``, so a refusal that only covered the
        bare path would send every real client straight through to the stream
        it was meant to stop, and the bill would not move.
        """
        app = app_for(api_database_url, offer_stream=False)

        async with serving(app) as base, httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{base}/mcp/", headers={"Accept": "text/event-stream"})

        assert response.status_code == 405
