"""The ASGI application: HTTP routes and the MCP server, in one process.

Both surfaces mount here and share one engine. That is the decision the whole
design rests on — two independently built query paths over one database would
drift, and the first symptom would be an agent and a developer getting
different answers to the same question.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from api import __version__
from api.config import Settings
from api.db import create_engine
from api.deps import AppState, SchemaCache
from api.errors import ProblemError, http_exception_handler, problem_handler, validation_handler
from api.logging import configure_logging
from api.routers import books, health

logger = structlog.get_logger(__name__)

DESCRIPTION = """\
A read-only API over a book catalogue built from Goodreads, Open Library,
Google Books and Project Gutenberg.

Served two ways over one repository layer: **HTTP** for developers, and
**MCP** at `/mcp` for AI agents.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the engine for the process lifetime.

    Created once at startup rather than per request: a pool built per request
    is not a pool. Disposed on shutdown so Cloud Run's scale-to-zero closes
    connections rather than leaving the database to time them out.
    """
    state: AppState = app.state.app_state
    logger.info("api.starting", version=__version__)
    try:
        yield
    finally:
        await state.engine.dispose()
        logger.info("api.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application.

    Takes settings rather than reading the environment so a test can stand up
    a second app against a different database without disturbing the first.
    """
    active = settings or Settings()  # type: ignore[call-arg]
    configure_logging(active.log_level)

    app = FastAPI(
        title="Book Catalogue API",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.state.app_state = AppState(
        settings=active,
        engine=create_engine(active),
        schema_cache=SchemaCache(ttl_seconds=active.readiness_cache_seconds),
    )

    app.add_exception_handler(ProblemError, problem_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)

    app.include_router(health.router)
    # /v1/books/search must be registered before /v1/books/{isbn13}, or the
    # literal path is captured by the parameterised one and search 400s as a
    # malformed ISBN. Ordering here is load-bearing, not cosmetic.
    app.include_router(books.router)

    return app


def run() -> None:
    """Console-script entry point."""
    import os  # noqa: PLC0415

    import uvicorn  # noqa: PLC0415

    # Cloud Run injects PORT and expects the process to honour it.
    uvicorn.run(
        "api.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
