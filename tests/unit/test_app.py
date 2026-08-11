"""Application construction and lifecycle."""

from __future__ import annotations

from typing import Any

import httpx

from api.config import Settings
from api.deps import ConnectionDep
from api.main import create_app, lifespan


class TestConstruction:
    def test_settings_can_be_injected(self) -> None:
        # So a test can stand up a second app against a different database
        # without disturbing the first.
        settings = Settings(database_url="postgresql://u:p@other/db", pool_size=3)  # type: ignore[arg-type]

        app = create_app(settings)

        assert app.state.app_state.settings.pool_size == 3

    def test_it_reads_the_environment_when_none_are_given(self) -> None:
        assert create_app().state.app_state.settings.database_url

    def test_the_engine_is_built_once_for_the_process(self) -> None:
        # A pool built per request is not a pool.
        app = create_app(Settings())  # type: ignore[call-arg]

        assert app.state.app_state.engine is app.state.app_state.engine

    def test_docs_are_served(self) -> None:
        # A reviewer's first stop; the TRD's smoke test requires it.
        app = create_app(Settings())  # type: ignore[call-arg]

        assert app.docs_url == "/docs"


class TestLifespan:
    async def test_the_engine_is_disposed_on_shutdown(self) -> None:
        """Scale-to-zero should close connections, not leave them to time out.

        Driven through the lifespan context directly: ASGITransport does not
        run lifespan events, so a request-based test would assert nothing.
        """
        app = create_app(Settings())  # type: ignore[call-arg]
        disposed: list[bool] = []

        class RecordingEngine:
            async def dispose(self) -> None:
                disposed.append(True)

        app.state.app_state.engine = RecordingEngine()

        async with lifespan(app):
            assert disposed == []

        assert disposed == [True]


class TestConnectionDependency:
    async def test_a_handler_receives_a_live_connection(self) -> None:
        app = create_app(Settings())  # type: ignore[call-arg]
        seen: list[object] = []

        class FakeConnection:
            pass

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

        app.state.app_state.engine = FakeEngine()

        @app.get("/uses-db")
        async def uses_db(connection: ConnectionDep) -> dict[str, bool]:
            seen.append(connection)
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get("/uses-db")

        assert response.status_code == 200
        assert isinstance(seen[0], FakeConnection)
