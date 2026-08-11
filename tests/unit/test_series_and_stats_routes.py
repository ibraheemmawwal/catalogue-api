"""Series and statistics endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from api.config import Settings
from api.deps import get_connection
from api.main import create_app


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    app = create_app(Settings())  # type: ignore[call-arg]

    async def fake_connection() -> Any:
        yield object()

    app.dependency_overrides[get_connection] = fake_connection
    state: dict[str, Any] = {}

    async def fake_get_series(_c: Any, *, series_id: int) -> Any:
        return state.get("series")

    async def fake_members(_c: Any, *, series_id: int) -> list[Any]:
        return state.get("members", [])

    async def fake_authors(_c: Any, ids: Any) -> dict[int, list[Any]]:
        return {}

    async def fake_coverage(_c: Any) -> Any:
        return state["coverage"]

    async def fake_relationships(_c: Any) -> Any:
        return SimpleNamespace(authors=3, subjects=4, series=1)

    async def fake_sources(_c: Any) -> list[Any]:
        return state.get("sources", [])

    async def fake_last_run(_c: Any) -> Any:
        return state.get("last_run")

    monkeypatch.setattr("api.repositories.series.get_series", fake_get_series)
    monkeypatch.setattr("api.repositories.series.members_of", fake_members)
    monkeypatch.setattr("api.repositories.books.authors_for", fake_authors)
    monkeypatch.setattr("api.repositories.stats.coverage", fake_coverage)
    monkeypatch.setattr("api.repositories.stats.relationship_counts", fake_relationships)
    monkeypatch.setattr("api.repositories.stats.per_source", fake_sources)
    monkeypatch.setattr("api.repositories.stats.last_successful_run", fake_last_run)

    app.state.fake = state
    return app


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def member(title: str, position: str | None, confirmed: bool) -> Any:
    return SimpleNamespace(
        id=1,
        isbn13=None,
        title=title,
        published_year=1965,
        language="eng",
        position=Decimal(position) if position else None,
        confirmed=confirmed,
    )


class TestSeries:
    async def test_a_series_returns_its_members(self, api: Any) -> None:
        api.state.fake["series"] = SimpleNamespace(id=1, name="Dune")
        api.state.fake["members"] = [member("Dune", "1", True)]

        async with client_for(api) as client:
            body = (await client.get("/v1/series/1")).json()

        assert body["name"] == "Dune"
        assert body["members"][0]["book"]["title"] == "Dune"

    async def test_confirmed_positions_are_counted(self, api: Any) -> None:
        api.state.fake["series"] = SimpleNamespace(id=1, name="Saga")
        api.state.fake["members"] = [member("A", "1", True), member("B", "2", False)]

        async with client_for(api) as client:
            body = (await client.get("/v1/series/1")).json()

        assert body["confirmed_positions"] == 1

    async def test_an_unknown_series_points_at_search(self, api: Any) -> None:
        api.state.fake["series"] = None

        async with client_for(api) as client:
            response = await client.get("/v1/series/999")

        assert response.status_code == 404
        assert "search" in response.json()["detail"].lower()

    async def test_a_non_numeric_id_is_rejected(self, api: Any) -> None:
        async with client_for(api) as client:
            response = await client.get("/v1/series/dune")

        assert response.status_code == 422


class TestStats:
    def _coverage(self, **overrides: Any) -> Any:
        base = {
            "books": 10,
            "with_isbn": 8,
            "with_year": 5,
            "with_publisher": 4,
            "with_page_count": 3,
            "with_cover": 2,
            "with_rating": 1,
            "earliest_year": 1818,
            "latest_year": 2020,
        }
        return SimpleNamespace(**{**base, **overrides})

    async def test_coverage_is_reported_as_a_percentage(self, api: Any) -> None:
        api.state.fake["coverage"] = self._coverage()

        async with client_for(api) as client:
            body = (await client.get("/v1/stats")).json()

        assert body["coverage"]["published_year"] == {"populated": 5, "percentage": 50.0}

    async def test_an_empty_catalogue_reports_zero_percent(self, api: Any) -> None:
        # A freshly deployed instance, before the first ingestion run.
        api.state.fake["coverage"] = self._coverage(
            books=0,
            with_isbn=0,
            with_year=0,
            with_publisher=0,
            with_page_count=0,
            with_cover=0,
            with_rating=0,
            earliest_year=None,
            latest_year=None,
        )

        async with client_for(api) as client:
            response = await client.get("/v1/stats")

        assert response.status_code == 200
        assert response.json()["coverage"]["isbn13"]["percentage"] == 0.0

    async def test_source_contributions_are_listed(self, api: Any) -> None:
        api.state.fake["coverage"] = self._coverage()
        api.state.fake["sources"] = [SimpleNamespace(source="goodreads", books=7)]

        async with client_for(api) as client:
            body = (await client.get("/v1/stats")).json()

        assert body["sources"][0] == {"source": "goodreads", "books": 7}

    async def test_the_last_run_is_reported(self, api: Any) -> None:
        # Staleness is invisible from the data itself.
        api.state.fake["coverage"] = self._coverage()
        api.state.fake["last_run"] = SimpleNamespace(
            started_at=datetime(2026, 8, 11, tzinfo=UTC), records_loaded=42
        )

        async with client_for(api) as client:
            body = (await client.get("/v1/stats")).json()

        assert body["last_run_records"] == 42
        assert body["last_run_at"].startswith("2026-08-11")

    async def test_no_runs_yet_is_not_an_error(self, api: Any) -> None:
        api.state.fake["coverage"] = self._coverage()

        async with client_for(api) as client:
            body = (await client.get("/v1/stats")).json()

        assert body["last_run_at"] is None
