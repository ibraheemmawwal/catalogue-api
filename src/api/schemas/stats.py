"""Statistics response shapes."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class FieldCoverage(BaseModel):
    """How complete one field is across the catalogue."""

    model_config = ConfigDict(frozen=True)

    populated: int
    percentage: float = Field(description="Rounded to one decimal place.")


class SourceContribution(BaseModel):
    """What one source supplied.

    Counts overlap: a book resolved by three sources appears under all three.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    books: int


class CatalogueStats(BaseModel):
    """What is in the catalogue, and how complete it is."""

    model_config = ConfigDict(frozen=True)

    books: int
    authors: int
    subjects: int
    series: int
    coverage: dict[str, FieldCoverage] = Field(
        description="Per-field completeness. Stated rather than assumed — the holes are real."
    )
    earliest_year: int | None = None
    latest_year: int | None = None
    sources: list[SourceContribution] = Field(default_factory=list)
    last_run_at: datetime | None = Field(
        default=None,
        description=(
            "When the catalogue last updated. Staleness is invisible from the "
            "data itself: a catalogue frozen six months ago looks like a fresh one."
        ),
    )
    last_run_records: int | None = None
