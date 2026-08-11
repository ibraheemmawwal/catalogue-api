"""A real PostgreSQL carrying the pipeline's real schema.

This service owns no migrations, so an integration test needs the schema from
the repository that does. It is applied here rather than reconstructed, and the
difference is not stylistic: the API's author and series filters use the
``%`` trigram operator, which does not exist without ``pg_trgm``, and
``books.search_vector`` is a generated four-band weighted tsvector. A schema
rebuilt from the column names in our contract would have neither — every search
test would pass against a fixture that behaves nothing like production.

The pin is a git ref plus an expected Alembic revision. The ref is what gets
applied; the revision assertion is what notices when the ref has moved
underneath us.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

# Pinned deliberately. An integration suite that tracks the pipeline's head
# would turn an unrelated upstream migration into a red build here, and a red
# build nobody caused is a red build nobody reads.
PIPELINE_SCHEMA_REF = os.environ.get("PIPELINE_SCHEMA_REF", "v2.0.1")
PINNED_ALEMBIC_REVISION = os.environ.get("PINNED_ALEMBIC_REVISION", "5979d87d772f")
PIPELINE_REPO = os.environ.get(
    "PIPELINE_REPO_URL", "https://github.com/ibraheemmawwal/book-data-pipeline.git"
)
# Set locally to skip the clone and use a working copy.
PIPELINE_LOCAL_PATH = os.environ.get("PIPELINE_LOCAL_PATH")


def _pipeline_checkout(tmp_root: Path) -> Path:
    """The pipeline source at the pinned ref."""
    if PIPELINE_LOCAL_PATH:
        return Path(PIPELINE_LOCAL_PATH)

    target = tmp_root / "book-data-pipeline"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            PIPELINE_SCHEMA_REF,
            PIPELINE_REPO,
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    return target


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A container with the pipeline's schema applied."""
    if shutil.which("docker") is None:
        pytest.skip("docker is required for integration tests")

    with PostgresContainer("postgres:16-alpine") as container:
        # Three drivers are in play and each needs its own spelling:
        # testcontainers reports psycopg2, the pipeline's Alembic runs on
        # psycopg (v3) inside its own environment, and this service uses
        # asyncpg. Spelling them explicitly beats a chain of replaces that
        # happens to work.
        base = container.get_connection_url().split("://", 1)[1]
        url = f"postgresql+psycopg://{base}"
        checkout = _pipeline_checkout(tmp_path_factory.mktemp("pipeline"))

        subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=checkout,
            check=True,
            capture_output=True,
            env={**os.environ, "PIPELINE_DATABASE_URL": url},
        )
        yield url


@pytest.fixture
def api_database_url(postgres_url: str) -> str:
    """The same database, spelled for the driver this service uses."""
    return "postgresql+asyncpg://" + postgres_url.split("://", 1)[1]


@pytest.fixture
def scalar(api_database_url: str) -> Callable[[str], Awaitable[Any]]:
    """Run one scalar query through the API's own driver.

    Deliberately asyncpg rather than a second sync driver: a fixture that
    reaches the database differently from the service can pass while the
    service cannot connect at all.
    """

    async def run(sql: str) -> Any:
        engine = create_async_engine(api_database_url)
        try:
            async with engine.connect() as connection:
                return (await connection.execute(text(sql))).scalar()
        finally:
            await engine.dispose()

    return run


@pytest.fixture
def scalars(api_database_url: str) -> Callable[[str], Awaitable[list[Any]]]:
    async def run(sql: str) -> list[Any]:
        engine = create_async_engine(api_database_url)
        try:
            async with engine.connect() as connection:
                return list((await connection.execute(text(sql))).scalars())
        finally:
            await engine.dispose()

    return run
