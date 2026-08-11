"""Series response shapes."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.books import BookSummary


class SeriesMember(BaseModel):
    """A book's place in a series."""

    model_config = ConfigDict(frozen=True)

    book: BookSummary
    position: Decimal | None = Field(
        default=None,
        description="Decimal, so a novella published as 4.5 keeps its place between 4 and 5.",
    )
    confirmed: bool = Field(
        description=(
            "True when a source stated this position; false when it was inferred "
            "from the title. Presented rather than flattened, because a guess and "
            "a statement are different claims."
        )
    )


class Series(BaseModel):
    """A series and its books, in reading order."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    members: list[SeriesMember] = Field(default_factory=list)
    confirmed_positions: int = Field(
        description="How many members have a position a source actually stated."
    )
