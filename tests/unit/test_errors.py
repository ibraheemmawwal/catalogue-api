"""Problem responses.

The rule under test: `detail` says what to do differently. A message that
restates the status code has told the caller nothing they did not already have.
"""

from __future__ import annotations

import httpx
import pytest

from api.config import Settings
from api.errors import ProblemError, invalid_request, not_found
from api.main import create_app


class TestNotFound:
    def test_it_names_the_identifier(self) -> None:
        error = not_found("Book", "9780000000000", hint="Use search_books to find its identifier.")

        assert "9780000000000" in error.detail

    def test_it_says_what_to_do_next(self) -> None:
        error = not_found("Book", "9780000000000", hint="Use search_books to find its identifier.")

        assert "search_books" in error.detail

    def test_it_carries_a_typed_problem_url(self) -> None:
        assert not_found("Book", "x", hint="y").problem_type.endswith("not-found")


class TestRendering:
    @pytest.fixture
    def client(self) -> httpx.AsyncClient:
        app = create_app(Settings())  # type: ignore[call-arg]

        @app.get("/boom")
        async def boom() -> None:
            raise not_found("Book", "9780000000000", hint="Try search.")

        @app.get("/bad")
        async def bad() -> None:
            raise invalid_request("year_from must be before year_to.")

        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def test_a_problem_uses_the_problem_media_type(self, client: httpx.AsyncClient) -> None:
        async with client:
            response = await client.get("/boom")

        assert response.headers["content-type"] == "application/problem+json"

    async def test_it_carries_the_rfc_9457_members(self, client: httpx.AsyncClient) -> None:
        async with client:
            body = (await client.get("/boom")).json()

        assert {"type", "title", "status", "detail", "instance"} <= set(body)
        assert body["status"] == 404
        assert body["instance"] == "/boom"

    async def test_a_bad_request_explains_the_constraint(self, client: httpx.AsyncClient) -> None:
        async with client:
            body = (await client.get("/bad")).json()

        assert body["status"] == 400
        assert "year_from" in body["detail"]

    async def test_routing_404s_use_the_same_shape(self, client: httpx.AsyncClient) -> None:
        # Otherwise a client parses two error formats from one service.
        async with client:
            response = await client.get("/no-such-route")

        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["status"] == 404


class TestValidationRendering:
    async def test_a_rejected_parameter_is_named_with_its_constraint(self) -> None:
        app = create_app(Settings())  # type: ignore[call-arg]

        @app.get("/items")
        async def items(limit: int) -> dict[str, int]:
            return {"limit": limit}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get("/items", params={"limit": "not-a-number"})

        assert response.status_code == 422
        body = response.json()
        assert response.headers["content-type"] == "application/problem+json"
        assert "limit" in body["detail"]
        assert body["errors"][0]["field"] == "limit"


class TestProblemError:
    def test_extras_reach_the_body(self) -> None:
        error = ProblemError(status_code=429, title="Too many", detail="Slow down.", retry_after=30)

        assert error.extras["retry_after"] == 30
