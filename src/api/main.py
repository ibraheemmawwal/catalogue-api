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
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException

from api import __version__
from api.config import Settings
from api.db import create_engine
from api.deps import AppState, SchemaCache
from api.errors import ProblemError, http_exception_handler, problem_handler, validation_handler
from api.logging import configure_logging
from api.mcp.server import build_mcp_server
from api.mcp.stream_policy import McpTransportMiddleware
from api.rate_limit import RateLimitMiddleware, RateLimitPolicy
from api.routers import books, health, search, series, stats

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

    # Starlette does not propagate lifespan to mounted sub-applications, and
    # the MCP session manager starts its task group there. Without this every
    # tool call fails with "Task group is not initialized" — the app starts
    # cleanly, the routes work, and only MCP is dead.
    mcp_lifespan = app.state.mcp_app.router.lifespan_context
    try:
        async with mcp_lifespan(app.state.mcp_app):
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

    # Added before the limiter and therefore wrapped *inside* it, which is the
    # order that matters: Starlette makes the last-added middleware outermost.
    # The limiter sees one request per call because this collapses the /mcp
    # redirect first.
    app.add_middleware(McpTransportMiddleware, refuse_stream=not active.mcp_offer_server_stream)

    # Outermost, so a refused request costs a comparison rather than a
    # database connection. Added before the routers only because ASGI
    # middleware wraps in reverse — this still runs first.
    if active.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            policy=RateLimitPolicy(
                per_minute=active.rate_limit_per_minute,
                burst=active.rate_limit_burst,
                mcp_per_minute=active.mcp_rate_limit_per_minute,
                mcp_burst=active.mcp_rate_limit_burst,
                trusted_proxies=active.rate_limit_trusted_proxies,
            ),
        )

    app.add_exception_handler(ProblemError, problem_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)

    app.include_router(health.router)
    # /v1/books/search must be registered before /v1/books/{isbn13}, or the
    # literal path is captured by the parameterised one and search 400s as a
    # malformed ISBN. Ordering here is load-bearing, not cosmetic.
    app.include_router(search.router)
    app.include_router(books.router)
    app.include_router(series.router)
    app.include_router(stats.router)

    # The same engine the routes use. Two independently built query paths over
    # one database would drift, and the first symptom would be an agent and a
    # developer getting different answers to the same question.
    mcp = build_mcp_server(app.state.app_state.engine, active)
    # Mounted at the root with the sub-app owning the "/mcp" path, rather than
    # mounted at "/mcp" — the sub-app already serves /mcp internally, so
    # mounting it there produces /mcp/mcp. Mounted last, so every REST route is
    # matched before this catch-all is reached.
    # Mounted at /mcp with the sub-app serving its own root, so the public
    # path is /mcp exactly once. The sub-app defaults to serving "/mcp"
    # internally, which mounted at /mcp would yield /mcp/mcp; mounting it at
    # the root instead makes it a catch-all that shadows every route
    # registered after it, including the problem-shaped 404 handler.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        # A single JSON body per POST instead of an SSE stream held open until
        # it is done. Every tool here is request-response, so a stream carries
        # one message and then waits — and Cloud Run bills a request for its
        # duration, so waiting is the expensive part.
        #
        # Refusing the GET stream was only half of it. With that refused the
        # client held a *POST* open for the same 61.000 seconds, because a POST
        # may answer with a stream too: the cost moved verbs rather than going
        # away. This is the half that closes it, and it makes the transport
        # consistent — no SSE on either verb, so nothing can be held at all.
        json_response=True,
        # Cloud Run round-robins across instances, so a session opened on one
        # can be continued on another that has never heard of it. Stateless
        # keeps every request self-contained, which is the only thing that
        # works behind a load balancer without session affinity.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=active.mcp_allowed_hosts,
            allowed_origins=["*"],
        ),
    )
    # Kept on state so the lifespan above can start its session manager.
    app.state.mcp_app = mcp_app
    app.mount("/mcp", mcp_app)

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
