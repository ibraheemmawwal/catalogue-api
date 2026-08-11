"""Probes, and the operational split they encode.

Liveness must not depend on the database. If it did, a database outage would
restart every healthy API process at once — turning a recoverable dependency
failure into a restart storm that makes recovery slower.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError

from api.config import Settings
from api.deps import SchemaCache
from api.main import create_app
from api.schema_contract import ContractResult


class FakeClock:
    """Advanceable time, so cache expiry is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def app_and_clock() -> tuple[Any, FakeClock]:
    app = create_app(Settings())  # type: ignore[call-arg]
    clock = FakeClock()
    app.state.app_state.clock = clock
    return app, clock


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def patch_contract(app: Any, monkeypatch: pytest.MonkeyPatch, result: ContractResult) -> list[int]:
    """Make the contract check succeed, counting how often it really ran."""
    calls: list[int] = []

    class FakeConnection:
        async def execute(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class FakeEngine:
        def connect(self) -> Any:
            class Ctx:
                async def __aenter__(self) -> FakeConnection:
                    return FakeConnection()

                async def __aexit__(self, *_exc: object) -> None:
                    return None

            return Ctx()

        async def dispose(self) -> None:
            return None

    async def fake_verify(_connection: Any) -> ContractResult:
        calls.append(1)
        return result

    app.state.app_state.engine = FakeEngine()
    monkeypatch.setattr("api.routers.health.verify_schema", fake_verify)
    return calls


class TestLiveness:
    async def test_it_answers_without_touching_the_database(
        self, app_and_clock: tuple[Any, FakeClock]
    ) -> None:
        # The engine is deliberately broken: a liveness probe that needs the
        # database is the bug this endpoint exists to avoid.
        app, _ = app_and_clock

        class ExplodingEngine:
            def connect(self) -> Any:
                raise AssertionError("liveness must not open a connection")

            async def dispose(self) -> None:
                return None

        app.state.app_state.engine = ExplodingEngine()

        async with client_for(app) as client:
            response = await client.get("/live")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_it_reports_the_running_version(
        self, app_and_clock: tuple[Any, FakeClock]
    ) -> None:
        app, _ = app_and_clock
        async with client_for(app) as client:
            body = (await client.get("/live")).json()

        assert body["version"]


class TestReadiness:
    async def test_a_compatible_schema_is_ready(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _ = app_and_clock
        patch_contract(app, monkeypatch, ContractResult(compatible=True))

        async with client_for(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "database": "ok", "schema": "compatible"}

    async def test_an_incompatible_schema_is_not_ready(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _ = app_and_clock
        patch_contract(
            app, monkeypatch, ContractResult(compatible=False, missing_columns=("books.isbn13",))
        )

        async with client_for(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        # The column name is the whole point: a bare 503 sends an operator to
        # the wrong repository.
        assert "books.isbn13" in response.json()["detail"]

    async def test_an_unreachable_database_is_not_ready(
        self, app_and_clock: tuple[Any, FakeClock]
    ) -> None:
        app, _ = app_and_clock

        class FailingEngine:
            def connect(self) -> Any:
                raise OperationalError("SELECT 1", {}, Exception("no route to host"))

            async def dispose(self) -> None:
                return None

        app.state.app_state.engine = FailingEngine()

        async with client_for(app) as client:
            response = await client.get("/ready")

        assert response.status_code == 503
        assert response.json()["database"] == "unreachable"


class TestReadinessCache:
    async def test_repeated_probes_do_not_requery(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Readiness is polled continuously; an information_schema scan per
        # probe is load added exactly when load is the problem.
        app, _ = app_and_clock
        calls = patch_contract(app, monkeypatch, ContractResult(compatible=True))

        async with client_for(app) as client:
            for _ in range(5):
                await client.get("/ready")

        assert len(calls) == 1

    async def test_the_cache_expires(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, clock = app_and_clock
        calls = patch_contract(app, monkeypatch, ContractResult(compatible=True))

        async with client_for(app) as client:
            await client.get("/ready")
            clock.advance(app.state.app_state.settings.readiness_cache_seconds + 1)
            await client.get("/ready")

        assert len(calls) == 2

    async def test_a_connection_failure_clears_a_cached_pass(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cached 'compatible' for a database we can no longer reach is a lie."""
        app, _ = app_and_clock
        patch_contract(app, monkeypatch, ContractResult(compatible=True))

        async with client_for(app) as client:
            assert (await client.get("/ready")).status_code == 200

            class FailingEngine:
                def connect(self) -> Any:
                    raise OperationalError("SELECT 1", {}, Exception("gone"))

                async def dispose(self) -> None:
                    return None

            app.state.app_state.engine = FailingEngine()

            assert (await client.get("/ready")).status_code == 503
            assert app.state.app_state.schema_cache.result is None


class TestHealth:
    async def test_it_aggregates(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _ = app_and_clock
        patch_contract(app, monkeypatch, ContractResult(compatible=True))

        async with client_for(app) as client:
            body = (await client.get("/health")).json()

        assert body == {
            "status": "ok",
            "database": "ok",
            "schema": "compatible",
            "version": body["version"],
        }

    async def test_it_degrades_without_a_database(
        self, app_and_clock: tuple[Any, FakeClock]
    ) -> None:
        app, _ = app_and_clock

        class FailingEngine:
            def connect(self) -> Any:
                raise OperationalError("SELECT 1", {}, Exception("gone"))

            async def dispose(self) -> None:
                return None

        app.state.app_state.engine = FailingEngine()

        async with client_for(app) as client:
            response = await client.get("/health")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"


class TestSchemaCacheUnit:
    def test_a_fresh_entry_is_returned(self) -> None:
        cache = SchemaCache(ttl_seconds=60)
        cache.put(ContractResult(compatible=True), now=100.0)

        assert cache.get(now=130.0) is not None

    def test_a_stale_entry_is_not(self) -> None:
        cache = SchemaCache(ttl_seconds=60)
        cache.put(ContractResult(compatible=True), now=100.0)

        assert cache.get(now=200.0) is None

    def test_an_empty_cache_returns_nothing(self) -> None:
        assert SchemaCache(ttl_seconds=60).get(now=1.0) is None


class TestHealthWithIncompatibleSchema:
    async def test_it_degrades_and_names_the_gap(
        self, app_and_clock: tuple[Any, FakeClock], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app, _ = app_and_clock
        patch_contract(
            app, monkeypatch, ContractResult(compatible=False, missing_tables=("series",))
        )

        async with client_for(app) as client:
            response = await client.get("/health")

        assert response.status_code == 503
        assert response.json()["schema"] == "incompatible"
        assert "series" in response.json()["detail"]
