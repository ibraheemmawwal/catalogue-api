"""Shared application state and request dependencies.

State lives on the app rather than in module globals so a test can build a
second app with a different database without the first one leaking into it.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api.config import Settings
from api.db import read_connection
from api.schema_contract import ContractResult


@dataclass
class SchemaCache:
    """The last contract result, and when it was taken.

    Readiness runs on every probe; re-querying information_schema each time
    costs a catalogue scan to re-learn something that changes only when the
    pipeline migrates. A connection failure clears it immediately, because a
    cached "compatible" from a database we can no longer reach is a lie.
    """

    ttl_seconds: float
    result: ContractResult | None = None
    checked_at: float = field(default=0.0)

    def get(self, now: float) -> ContractResult | None:
        if self.result is None or now - self.checked_at > self.ttl_seconds:
            return None
        return self.result

    def put(self, result: ContractResult, now: float) -> None:
        self.result, self.checked_at = result, now

    def invalidate(self) -> None:
        self.result = None


@dataclass
class AppState:
    """Everything a request handler may need."""

    settings: Settings
    engine: AsyncEngine
    schema_cache: SchemaCache
    clock: object = time.monotonic


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.app_state
    return state


async def get_connection(
    state: Annotated[AppState, Depends(get_state)],
) -> AsyncIterator[AsyncConnection]:
    async with read_connection(state.engine) as connection:
        yield connection


StateDep = Annotated[AppState, Depends(get_state)]
ConnectionDep = Annotated[AsyncConnection, Depends(get_connection)]
