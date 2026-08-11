"""The statistics endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from api.deps import ConnectionDep
from api.repositories import stats as repo
from api.schemas.stats import CatalogueStats, FieldCoverage, SourceContribution

router = APIRouter(prefix="/v1", tags=["stats"])

# Reported field -> the counted column on the coverage row.
_COVERED_FIELDS = {
    "isbn13": "with_isbn",
    "published_year": "with_year",
    "publisher": "with_publisher",
    "page_count": "with_page_count",
    "cover_url": "with_cover",
    "goodreads_average_rating": "with_rating",
}


def _percentage(populated: int, total: int) -> float:
    # An empty catalogue is 0%, not a division error. A freshly deployed
    # instance hits this before the first ingestion run.
    return round(populated / total * 100, 1) if total else 0.0


@router.get("/stats", response_model=CatalogueStats, summary="Catalogue statistics")
async def get_stats(connection: ConnectionDep) -> CatalogueStats:
    """Counts, coverage and provenance."""
    counts = await repo.coverage(connection)
    relationships = await repo.relationship_counts(connection)
    sources = await repo.per_source(connection)
    last_run = await repo.last_successful_run(connection)

    return CatalogueStats(
        books=counts.books,
        authors=relationships.authors,
        subjects=relationships.subjects,
        series=relationships.series,
        coverage={
            field: FieldCoverage(
                populated=getattr(counts, column),
                percentage=_percentage(getattr(counts, column), counts.books),
            )
            for field, column in _COVERED_FIELDS.items()
        },
        earliest_year=counts.earliest_year,
        latest_year=counts.latest_year,
        sources=[SourceContribution(source=row.source, books=row.books) for row in sources],
        last_run_at=last_run.started_at if last_run else None,
        last_run_records=last_run.records_loaded if last_run else None,
    )
