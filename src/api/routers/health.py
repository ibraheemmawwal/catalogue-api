"""Liveness, readiness and the human-readable aggregate.

The split matters operationally: liveness answers "is this process alive",
readiness answers "can it serve traffic". Conflating them means a database
outage restarts every healthy API process, turning a recoverable dependency
failure into a thundering-herd restart loop.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api import __version__
from api.db import read_connection
from api.deps import StateDep
from api.schema_contract import ContractResult, verify_schema

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/live", summary="Liveness")
async def live() -> dict[str, str]:
    """Process liveness. Touches nothing external, by design."""
    return {"status": "ok", "version": __version__}


async def _current_contract(state: StateDep) -> ContractResult | None:
    """Reachability now; schema compatibility at most once per TTL.

    Only the *compatibility* result is cached, never reachability. The two have
    completely different costs and change on completely different timescales:
    ``SELECT 1`` is a round trip, while the contract check scans
    ``information_schema`` and can only change when the pipeline migrates.

    Caching both was a real bug — readiness kept answering 200 for a full TTL
    after the database went away, which is precisely the window in which a load
    balancer should have stopped sending traffic to this instance.

    Returns None when the database is unreachable.
    """
    now = float(state.clock())  # type: ignore[operator]

    try:
        async with read_connection(state.engine) as connection:
            # Every probe: cheap, and the only thing that proves reachability.
            await connection.execute(text("SELECT 1"))

            cached = state.schema_cache.get(now)
            if cached is not None:
                return cached

            result = await verify_schema(connection)
    except (SQLAlchemyError, OSError) as error:
        # A cached pass for a database we cannot reach is a lie; drop it.
        state.schema_cache.invalidate()
        logger.warning("readiness.database_unreachable", error=str(error))
        return None

    state.schema_cache.put(result, now)
    return result


@router.get("/ready", summary="Readiness")
async def ready(state: StateDep, response: Response) -> dict[str, Any]:
    """Whether this instance can serve real traffic."""
    contract = await _current_contract(state)

    if contract is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "database": "unreachable"}

    if not contract.compatible:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        # The specifics go in the body: an operator seeing this needs the
        # column name, and a 503 alone sends them to the wrong repository.
        return {
            "status": "unavailable",
            "database": "ok",
            "schema": "incompatible",
            "detail": contract.describe(),
        }

    return {"status": "ok", "database": "ok", "schema": "compatible"}


@router.get("/health", summary="Aggregate health")
async def health(state: StateDep, response: Response) -> dict[str, Any]:
    """Human-readable aggregate. Explicitly not the liveness probe."""
    contract = await _current_contract(state)

    if contract is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "degraded",
            "database": "unreachable",
            "schema": "unknown",
            "version": __version__,
        }

    if not contract.compatible:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "degraded",
            "database": "ok",
            "schema": "incompatible",
            "detail": contract.describe(),
            "version": __version__,
        }

    return {
        "status": "ok",
        "database": "ok",
        "schema": "compatible",
        "version": __version__,
    }
