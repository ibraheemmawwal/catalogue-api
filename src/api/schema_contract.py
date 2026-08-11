"""The pinned contract against the pipeline's schema.

This service owns no migrations. It reads tables another repository creates,
which makes the coupling real and worth naming: a column renamed upstream would
otherwise surface here as a 500 on one endpoint, in production, days later.

So the contract is explicit and checked at startup. It lists only what the API
actually reads — not the pipeline's whole schema. Pinning columns nobody selects
would turn every unrelated upstream migration into a false alarm here, and an
alarm that cries wolf gets muted.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Read as: table -> the columns this API selects from it.
REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "books": frozenset(
        {
            "id",
            "isbn13",
            "identity_key",
            "title",
            "subtitle",
            "published_year",
            "publisher",
            "page_count",
            "language",
            "cover_url",
            "goodreads_average_rating",
            "download_count",
            "search_vector",
            "updated_at",
        }
    ),
    "authors": frozenset({"id", "name"}),
    "book_authors": frozenset({"book_id", "author_id"}),
    "subjects": frozenset({"id", "name", "normalized_name"}),
    "book_subjects": frozenset({"book_id", "subject_id"}),
    "series": frozenset({"id", "name"}),
    "book_series": frozenset({"book_id", "series_id", "position", "confirmed"}),
    # Provenance. The MCP surface's reason to exist, so its columns are part of
    # the contract rather than a best-effort read.
    "book_sources": frozenset({"book_id", "source", "source_id", "fetched_at"}),
    "ingestion_runs": frozenset({"id", "status", "started_at", "records_loaded"}),
}


@dataclass(frozen=True, slots=True)
class ContractResult:
    """What the check found, in a shape an operator can act on."""

    compatible: bool
    missing_tables: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()

    def describe(self) -> str:
        """A one-line summary naming what is wrong, not that something is."""
        if self.compatible:
            return "compatible"
        parts = []
        if self.missing_tables:
            parts.append(f"missing tables: {', '.join(self.missing_tables)}")
        if self.missing_columns:
            parts.append(f"missing columns: {', '.join(self.missing_columns)}")
        return "; ".join(parts)


async def verify_schema(connection: AsyncConnection) -> ContractResult:
    """Check the live database against the contract.

    One query rather than one per table: this runs on every readiness probe,
    and a probe that issues ten round trips is a probe that fails under the
    load it is meant to be reporting on.
    """
    rows = await connection.execute(
        text(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(:tables)
            """
        ),
        {"tables": list(REQUIRED_SCHEMA)},
    )

    found: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        found.setdefault(table_name, set()).add(column_name)

    missing_tables = tuple(sorted(name for name in REQUIRED_SCHEMA if name not in found))
    missing_columns = tuple(
        sorted(
            f"{table}.{column}"
            for table, columns in REQUIRED_SCHEMA.items()
            if table in found
            for column in columns - found[table]
        )
    )

    return ContractResult(
        compatible=not (missing_tables or missing_columns),
        missing_tables=missing_tables,
        missing_columns=missing_columns,
    )
