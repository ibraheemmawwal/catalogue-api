"""The limiter over the real app, including the mounted MCP sub-application.

The unit tests cover the counting. What they cannot show is that the
middleware is actually in the request path for both surfaces — and the MCP one
is the reason this is pure ASGI rather than ``BaseHTTPMiddleware``, which
buffers responses and would break the transport's streaming.

Also here: that health probes are never limited, and that a limited caller
gets the same problem+json shape as every other error rather than a bare 429
from somewhere in the stack.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api.config import Settings
from api.main import create_app

pytestmark = pytest.mark.integration


def limited_app(database_url: str, **overrides: Any) -> Any:
    settings = Settings(  # type: ignore[call-arg]
        database_url=database_url.replace("+asyncpg", ""),
        rate_limit_enabled=True,
        rate_limit_per_minute=60,
        rate_limit_burst=3,
        mcp_rate_limit_per_minute=60,
        mcp_rate_limit_burst=2,
        # The peer address, so the test client is the whole identity.
        rate_limit_trusted_proxies=0,
        **overrides,
    )
    return create_app(settings)


async def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


class TestTheRestSurface:
    async def test_a_caller_over_the_budget_is_refused(
        self, seeded: Any, api_database_url: str
    ) -> None:
        app = limited_app(api_database_url)
        async with await client_for(app) as client:
            codes = [(await client.get("/v1/books")).status_code for _ in range(5)]

        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429

    async def test_the_refusal_is_shaped_like_every_other_error(
        self, seeded: Any, api_database_url: str
    ) -> None:
        """One error format, or a client parses two.

        The middleware sits outside the exception handlers, so this shape is
        written by hand and would silently drift from the handlers' unless
        something checked.
        """
        app = limited_app(api_database_url)
        async with await client_for(app) as client:
            for _ in range(4):
                response = await client.get("/v1/books")

        assert response.status_code == 429
        assert response.headers["content-type"].startswith("application/problem+json")
        assert int(response.headers["retry-after"]) >= 1
        body = response.json()
        assert body["status"] == 429
        assert body["instance"] == "/v1/books"


class TestTheMcpSurface:
    async def test_it_has_its_own_smaller_budget(self, seeded: Any, api_database_url: str) -> None:
        """MCP is limited separately, and sooner.

        A REST call runs a query we wrote; an MCP call can carry one the
        caller wrote. Two budgets is the difference between pricing them the
        same and pricing them by what they cost.
        """
        app = limited_app(api_database_url)
        headers = {"Accept": "application/json, text/event-stream"}
        async with await client_for(app) as client:
            mcp = [
                (await client.post("/mcp", json={}, headers=headers)).status_code for _ in range(4)
            ]
            rest = (await client.get("/v1/books")).status_code

        assert 429 in mcp, "the MCP mount is not behind the limiter"
        # Spending the MCP budget must not spend the REST one.
        assert rest == 200


class TestWhatIsNeverLimited:
    @pytest.mark.parametrize("path", ["/live", "/ready"])
    async def test_health_probes_are_exempt(
        self, seeded: Any, api_database_url: str, path: str
    ) -> None:
        """A limited probe reads as an unhealthy instance.

        Cloud Run would take it out of rotation for being busy, which turns a
        traffic spike into a smaller fleet — exactly backwards.
        """
        app = limited_app(api_database_url)
        async with await client_for(app) as client:
            codes = [(await client.get(path)).status_code for _ in range(10)]

        assert 429 not in codes


class TestTurningItOff:
    async def test_disabled_means_no_middleware_at_all(
        self, seeded: Any, api_database_url: str
    ) -> None:
        # The escape hatch has to actually work: behind a gateway that already
        # limits, a second per-instance limit is a confusing extra failure.
        settings = Settings(  # type: ignore[call-arg]
            database_url=api_database_url.replace("+asyncpg", ""),
            rate_limit_enabled=False,
        )
        app = create_app(settings)

        async with await client_for(app) as client:
            codes = [(await client.get("/v1/books")).status_code for _ in range(40)]

        assert set(codes) == {200}
