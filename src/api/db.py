"""Engine and session management.

The pool is small on purpose. Neon's pooled endpoint already pools; a large
client-side pool on top multiplies connections rather than reusing them, and
Cloud Run scales by adding processes — each with its own pool. Ten instances
with a pool of five is fifty connections, which is the number that matters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from api.config import Settings

logger = structlog.get_logger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the engine for this process."""
    return create_async_engine(
        settings.async_database_url(),
        pool_size=settings.pool_size,
        max_overflow=settings.pool_max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # Scale-to-zero means a connection can outlive the proxy that opened
        # it; a recycled connection beats discovering that mid-request.
        pool_recycle=1_800,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.statement_timeout_ms),
                # Names this service in pg_stat_activity, so a DBA looking at a
                # slow query knows which client to ask about.
                "application_name": "catalogue-api",
            },
            "timeout": settings.pool_timeout_seconds,
        },
    )


@asynccontextmanager
async def read_connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection for one read.

    No transaction is opened. Every query this service runs is a single
    statement, and wrapping each in an explicit transaction would add a round
    trip per request to isolate reads that cannot see each other anyway.
    """
    async with engine.connect() as connection:
        yield connection
