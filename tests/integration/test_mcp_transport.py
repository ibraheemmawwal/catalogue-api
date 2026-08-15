"""The transport itself, rather than the tools it carries.

``test_mcp_tools`` drives every tool over a real Streamable HTTP session, so
mounting, schema generation and serialisation are already exercised. What it
does not ask is what the transport does when a request is *not* a well-formed
tool call from a well-behaved client — and that is the whole of the public
surface, because /mcp is unauthenticated and reachable by anything.

The questions here are: does the mount answer where we claim it does, does a
malformed or hostile request produce a protocol error rather than a stack
trace, and does a stateless server really carry no state between sessions.

That last one is not theoretical. The server is built ``stateless_http=True``
specifically because Cloud Run round-robins across instances, so a session
continued on an instance that never saw it must still work. A test that only
ever opens one session cannot tell that apart from a server that is quietly
stateful and happens to be running as a single process.
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

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@asynccontextmanager
async def serving(database_url: str) -> AsyncIterator[str]:
    """The real app on a real socket, yielding its base URL."""
    settings = Settings(database_url=database_url.replace("+asyncpg", ""))  # type: ignore[arg-type]
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def session_at(base: str) -> AsyncIterator[ClientSession]:
    async with (
        streamable_http_client(f"{base}/mcp") as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


class TestWhereItIsMounted:
    async def test_the_mount_answers_at_mcp_exactly_once(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """/mcp, not /mcp/mcp.

        The sub-app serves its own root and is mounted at /mcp; get that pairing
        wrong and the public path doubles. main.py carries two comments about
        it, which is a fair sign it has been got wrong before.
        """
        async with serving(api_database_url) as base, httpx.AsyncClient() as client:
            doubled = await client.post(f"{base}/mcp/mcp", json={}, headers=MCP_HEADERS)

        assert doubled.status_code == 404

    async def test_the_rest_routes_still_win(self, seeded: Any, api_database_url: str) -> None:
        """The mount is a catch-all and is mounted last for that reason.

        If it were reached first it would shadow every REST route, and the
        symptom would be the API returning MCP protocol errors for /v1 paths.
        """
        async with serving(api_database_url) as base, httpx.AsyncClient() as client:
            live = await client.get(f"{base}/live")
            missing = await client.get(f"{base}/v1/nothing-here")

        assert live.status_code == 200
        assert missing.status_code == 404
        # The problem-shaped handler, not the MCP sub-app's error.
        assert missing.headers["content-type"].startswith("application/problem+json")


class TestAnIllFormedRequest:
    async def test_a_non_mcp_body_is_a_protocol_error_not_a_crash(
        self, seeded: Any, api_database_url: str
    ) -> None:
        async with serving(api_database_url) as base, httpx.AsyncClient() as client:
            response = await client.post(
                f"{base}/mcp", json={"not": "jsonrpc"}, headers=MCP_HEADERS
            )

        assert response.status_code < 500, response.text

    async def test_nothing_leaks_a_traceback(self, seeded: Any, api_database_url: str) -> None:
        """A stack trace on an unauthenticated endpoint is a free map.

        It names the framework, the versions and the file layout, and this
        surface is reachable by anything that can make an HTTP request.
        """
        async with serving(api_database_url) as base, httpx.AsyncClient() as client:
            responses = [
                await client.post(f"{base}/mcp", content=b"{", headers=MCP_HEADERS),
                await client.post(f"{base}/mcp", json={"jsonrpc": "2.0"}, headers=MCP_HEADERS),
                await client.get(f"{base}/mcp", headers=MCP_HEADERS),
            ]

        for response in responses:
            body = response.text.lower()
            assert "traceback" not in body
            assert "site-packages" not in body
            assert 'file "/' not in body

    async def test_an_unknown_tool_is_reported_to_the_caller(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # An agent that guesses a tool name should be told, not disconnected.
        async with serving(api_database_url) as base, session_at(base) as session:
            result = await session.call_tool("no_such_tool", {})

        assert result.is_error

    async def test_a_missing_argument_comes_back_as_guidance(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """Not a protocol error, deliberately.

        ``get_book`` takes either an ISBN or an id and omitting both is the
        obvious first mistake. A protocol error would tell the agent the call
        failed; a structured message tells it what to send instead and which
        tool finds it, which is the difference between an agent that stops and
        one that recovers.
        """
        async with serving(api_database_url) as base, session_at(base) as session:
            result = await session.call_tool("get_book", {})

        assert result.is_error is False
        assert result.structured_content is not None
        assert "search_books" in result.structured_content["error"]


class TestStatelessness:
    async def test_two_sessions_do_not_share_anything(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """The property Cloud Run actually requires.

        Sessions are served without affinity, so one that carried state across
        requests would work in every single-instance test and fail in
        production under load — the worst shape of bug to find late.
        """
        await seeded.book("Dune", isbn13="9780553380163", year=1965)

        async with serving(api_database_url) as base:
            async with session_at(base) as first:
                first_tools = {t.name for t in (await first.list_tools()).tools}
                first_call = await first.call_tool("catalogue_stats", {})

            async with session_at(base) as second:
                second_tools = {t.name for t in (await second.list_tools()).tools}
                second_call = await second.call_tool("catalogue_stats", {})

        assert first_tools == second_tools
        assert first_call.structured_content == second_call.structured_content

    async def test_concurrent_sessions_do_not_interfere(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # Overlapping rather than sequential: the session manager runs one task
        # group for the process, and two live sessions share it.
        await seeded.book("Dune", isbn13="9780553380163", year=1965)

        async with serving(api_database_url) as base:

            async def stats() -> Any:
                async with session_at(base) as session:
                    return (await session.call_tool("catalogue_stats", {})).structured_content

            results = await asyncio.gather(*(stats() for _ in range(4)))

        assert all(r == results[0] for r in results)
