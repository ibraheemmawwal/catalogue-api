"""Runtime configuration.

Boundary validation, same discipline as the pipeline: a misconfigured service
fails at startup with a named field rather than at the first request with a
stack trace.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "API_"


class Settings(BaseSettings):
    """Everything the service reads from its environment."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="forbid",
        frozen=True,
    )

    database_url: PostgresDsn
    # Neon's pooled endpoint does its own pooling, so a large client-side pool
    # multiplies connections rather than reusing them. Cloud Run scales by
    # process; the per-process pool stays deliberately small.
    pool_size: int = Field(default=5, ge=1, le=20)
    pool_max_overflow: int = Field(default=2, ge=0, le=10)
    pool_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # A read that has run this long is a query nobody is still waiting for.
    statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)

    default_page_size: int = Field(default=20, ge=1, le=100)
    max_page_size: int = Field(default=100, ge=1, le=500)

    # An MCP result lands directly in a model's context window, so this is a
    # context-budget decision rather than a database one — which is why it is
    # far lower than max_page_size.
    mcp_max_results: int = Field(default=50, ge=1, le=100)
    mcp_default_results: int = Field(default=10, ge=1, le=50)

    readiness_cache_seconds: float = Field(default=60.0, ge=0, le=300)
    log_level: str = Field(default="INFO")

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = value.upper()
        if upper not in allowed:
            msg = f"log_level must be one of {sorted(allowed)}"
            raise ValueError(msg)
        return upper

    @model_validator(mode="after")
    def _page_sizes_agree(self) -> Settings:
        if self.default_page_size > self.max_page_size:
            msg = "default_page_size cannot exceed max_page_size"
            raise ValueError(msg)
        if self.mcp_default_results > self.mcp_max_results:
            msg = "mcp_default_results cannot exceed mcp_max_results"
            raise ValueError(msg)
        return self

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_prefixed_variables(cls, data: Any) -> Any:
        """Fail on an ``API_*`` variable that matches no field.

        ``extra="forbid"`` cannot catch these: the environment source only ever
        reads variables it already has a field for, so a typo like
        ``API_POOL_SIZ`` is silently ignored and the default is used instead —
        the service starts, looks healthy, and is misconfigured.
        """
        known = {f"{ENV_PREFIX}{name.upper()}" for name in cls.model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
        )
        if unknown:
            msg = f"unknown {ENV_PREFIX}* environment variables: {', '.join(unknown)}"
            raise ValueError(msg)
        return data

    def async_database_url(self) -> str:
        """The URL with the async driver SQLAlchemy needs.

        Operators paste whatever their provider hands them, which is a
        ``postgresql://`` URL. Rewriting it here beats failing at connect time
        with a driver error nobody wrote.
        """
        url = str(self.database_url)
        for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url[len(prefix) :]
        return url
