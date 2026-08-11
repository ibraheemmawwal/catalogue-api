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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

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


@pytest.fixture
async def seeded(api_database_url: str) -> AsyncIterator[Any]:
    """A catalogue with rows, truncated between tests.

    Every write commits before the fixture yields control. Holding one open
    transaction across the test would deadlock it: the TRUNCATE takes an
    exclusive lock, and the test queries on a separate connection, which then
    waits on a transaction that cannot commit until the test finishes.

    Rows go in through raw SQL rather than the pipeline's loader. This suite
    asks whether *our* queries are right against the real schema; routing the
    setup through the pipeline's write path would let a load-layer bug read as
    an API bug.
    """
    engine = create_async_engine(api_database_url)

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE books, authors, subjects, series, book_authors, "
                "book_subjects, book_series RESTART IDENTITY CASCADE"
            )
        )

    class Seeder:
        async def book(
            self,
            title: str,
            *,
            isbn13: str | None = None,
            year: int | None = None,
            language: str | None = "eng",
        ) -> int:
            async with engine.begin() as connection:
                row = await connection.execute(
                    text(
                        """
                        INSERT INTO books (identity_key, isbn13, title, published_year,
                                           language, content_hash)
                        VALUES (:key, :isbn13, :title, :year, :language, :hash)
                        RETURNING id
                        """
                    ),
                    {
                        # Unique per row: several tests insert the same title
                        # on purpose, and identity_key is unique upstream.
                        "key": f"test:{title}:{uuid4()}",
                        "isbn13": isbn13,
                        "title": title,
                        "year": year,
                        "language": language,
                        "hash": f"hash-{uuid4()}",
                    },
                )
                return int(row.scalar_one())

        async def author(self, book_id: int, name: str) -> None:
            async with engine.begin() as connection:
                author_id = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO authors (name, normalized_name)
                            VALUES (:name, :norm)
                            ON CONFLICT (normalized_name)
                            DO UPDATE SET name = EXCLUDED.name
                            RETURNING id
                            """
                        ),
                        {"name": name, "norm": name.lower()},
                    )
                ).scalar_one()
                await connection.execute(
                    text(
                        "INSERT INTO book_authors (book_id, author_id) "
                        "VALUES (:b, :a) ON CONFLICT DO NOTHING"
                    ),
                    {"b": book_id, "a": author_id},
                )

        async def subject(self, book_id: int, name: str) -> None:
            async with engine.begin() as connection:
                subject_id = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO subjects (name, normalized_name)
                            VALUES (:name, :norm)
                            ON CONFLICT (normalized_name)
                            DO UPDATE SET name = EXCLUDED.name
                            RETURNING id
                            """
                        ),
                        {"name": name, "norm": name.lower()},
                    )
                ).scalar_one()
                await connection.execute(
                    text(
                        "INSERT INTO book_subjects (book_id, subject_id) "
                        "VALUES (:b, :s) ON CONFLICT DO NOTHING"
                    ),
                    {"b": book_id, "s": subject_id},
                )

        async def series(
            self, book_id: int, name: str, *, position: str | None = None, confirmed: bool = False
        ) -> int:
            async with engine.begin() as connection:
                series_id = (
                    await connection.execute(
                        text(
                            """
                            INSERT INTO series (identity_key, name, normalized_name)
                            VALUES (:key, :name, :norm)
                            ON CONFLICT (identity_key)
                            DO UPDATE SET name = EXCLUDED.name
                            RETURNING id
                            """
                        ),
                        {"key": f"series:{name.lower()}", "name": name, "norm": name.lower()},
                    )
                ).scalar_one()
                await connection.execute(
                    text(
                        """
                        INSERT INTO book_series (book_id, series_id, position, confirmed)
                        VALUES (:b, :s, :p, :c) ON CONFLICT DO NOTHING
                        """
                    ),
                    {"b": book_id, "s": series_id, "p": position, "c": confirmed},
                )
                return int(series_id)

        async def source(self, book_id: int, name: str, source_id: str = "x") -> None:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO book_sources
                            (book_id, source, source_id, raw_payload, payload_hash)
                        VALUES (:b, :src, :sid, '{}'::jsonb, :hash)
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "b": book_id,
                        "src": name,
                        "sid": f"{source_id}-{book_id}",
                        "hash": f"h-{uuid4()}",
                    },
                )

    yield Seeder()

    await engine.dispose()
