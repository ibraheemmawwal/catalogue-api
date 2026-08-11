"""Book response shapes.

Two shapes on purpose. A collection row carries what a list needs; a detail
record carries everything. Serving the detail shape from the collection would
multiply every list response by the size of its subject and author arrays, for
fields a list view does not render.

That split is worth more on the MCP surface than on HTTP: a tool result goes
straight into a model's context window, so an unnecessary field is a recurring
cost paid on every call rather than a few bytes on the wire.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class AuthorRef(BaseModel):
    """An author as it appears on a book."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str


class SeriesRef(BaseModel):
    """A book's place in a series."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    position: Decimal | None = Field(
        default=None,
        description="Decimal, so a novella published as 4.5 keeps its place.",
    )
    confirmed: bool = Field(
        description=(
            "True when a source stated this position, false when it was inferred "
            "from a title pattern. A different strength of claim, so it is not "
            "flattened away."
        )
    )


class BookSummary(BaseModel):
    """A book in a collection."""

    model_config = ConfigDict(frozen=True)

    id: int
    isbn13: str | None = None
    title: str
    authors: list[str] = Field(default_factory=list)
    published_year: int | None = None
    language: str | None = None


class Book(BaseModel):
    """One book in full."""

    model_config = ConfigDict(frozen=True)

    id: int
    isbn13: str | None = None
    title: str
    subtitle: str | None = None
    authors: list[AuthorRef] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    series: list[SeriesRef] = Field(default_factory=list)
    published_year: int | None = None
    publisher: str | None = None
    page_count: int | None = None
    language: str | None = None
    cover_url: str | None = None
    goodreads_average_rating: Decimal | None = None
    download_count: int | None = None


class BookPage(BaseModel):
    """A page of books, and how to ask for the next one."""

    model_config = ConfigDict(frozen=True)

    items: list[BookSummary]
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Opaque. Pass it back as `cursor` for the next page; absent means "
            "this was the last one."
        ),
    )
