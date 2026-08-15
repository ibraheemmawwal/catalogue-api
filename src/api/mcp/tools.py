"""The eight tools.

Every one calls the same repository functions the HTTP routes call. A tool that
reached past the repository into SQL of its own would be exactly the drift the
shared layer exists to prevent.

Responses are shaped for a context window rather than a wire: null fields are
dropped, long lists truncate with a remainder count, and search returns a
projection. An unnecessary field in a tool result is not a few bytes once — it
is a cost paid on every call, in the space a model has left to think.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from api.config import Settings
from api.repositories import books as book_repo
from api.repositories import contested as contested_repo
from api.repositories import introspection as introspection_repo
from api.repositories import provenance as provenance_repo
from api.repositories import search as search_repo
from api.repositories import series as series_repo
from api.repositories import stats as stats_repo
from api.validators import normalise_isbn13

# Beyond this a subject list is noise in a context window rather than signal.
MAX_SUBJECTS = 12


def compact(record: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values.

    `"publisher": null` tells a model nothing it could not infer from the key
    being absent, and it costs tokens on every record of every call.
    """
    return {
        key: value for key, value in record.items() if value is not None and value not in ([], {})
    }


def _number(value: Decimal | None) -> float | str | None:
    """Render a decimal for JSON without pretending it is a float.

    Positions and ratings are NUMERIC. Sent as floats they acquire trailing
    noise a model then reports back verbatim.
    """
    return None if value is None else str(value.normalize())


class CatalogueTools:
    """Tool implementations bound to one engine."""

    def __init__(self, engine: AsyncEngine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    async def search_books(
        self,
        query: str | None = None,
        author: str | None = None,
        subject: str | None = None,
        series: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find books. See descriptions.SEARCH_BOOKS."""
        capped = max(1, min(limit, self._settings.mcp_max_results))

        if year_from is not None and year_to is not None and year_from > year_to:
            return {
                "error": (
                    f"year_from ({year_from}) is after year_to ({year_to}); no book "
                    "can match. Swap them or drop one."
                )
            }

        async with self._engine.connect() as connection:
            if query:
                # Over-fetch so "more than shown" is answerable without a
                # second count over the same predicate.
                rows, _ = await search_repo.search(connection, query=query, limit=capped * 3)
            else:
                rows = await book_repo.list_books(
                    connection,
                    filters=book_repo.BookFilters(
                        author=author,
                        subject=subject,
                        series=series,
                        language=language,
                        year_from=year_from,
                        year_to=year_to,
                    ),
                    limit=capped * 3,
                )

            shown = rows[:capped]
            authors = await book_repo.authors_for(connection, [row.id for row in shown])

        matches = [
            compact(
                {
                    "id": row.id,
                    "isbn13": row.isbn13,
                    "title": row.title,
                    "authors": [a.name for a in authors.get(row.id, [])],
                    "year": row.published_year,
                }
            )
            for row in shown
        ]

        result: dict[str, Any] = {"matches": matches, "shown": len(matches)}
        if len(rows) > capped:
            # Deliberately not a cursor. An agent threading an opaque token
            # across turns loses its place; telling it the result set is larger
            # prompts a narrower query, which is the behaviour we want.
            result["more_available"] = True
            result["hint"] = "More books match. Narrow the search with author, subject or year."
        return result

    async def get_book(self, isbn13: str | None = None, id: int | None = None) -> dict[str, Any]:  # noqa: A002
        """One book in full. See descriptions.GET_BOOK."""
        if isbn13 is None and id is None:
            return {"error": "Provide either isbn13 or id. Use search_books to find one."}

        async with self._engine.connect() as connection:
            if isbn13 is not None:
                normalised = normalise_isbn13(isbn13)
                if normalised is None:
                    return {
                        "error": (
                            f"{isbn13!r} is not a valid ISBN-13 — it needs 13 digits with a "
                            "correct check digit. Use search_books to find the right one."
                        )
                    }
                row = await book_repo.get_book(connection, isbn13=normalised)
                identifier = normalised
            else:
                row = await book_repo.get_book_by_id(connection, book_id=id)  # type: ignore[arg-type]
                identifier = str(id)

            if row is None:
                return {
                    "error": (
                        f"No book found for {identifier}. Use search_books to find its identifier."
                    )
                }

            authors = (await book_repo.authors_for(connection, [row.id])).get(row.id, [])
            subjects = await book_repo.subjects_for(connection, row.id)
            series = await book_repo.series_for(connection, row.id)

        record = compact(
            {
                "id": row.id,
                "isbn13": row.isbn13,
                "title": row.title,
                "subtitle": row.subtitle,
                "authors": [a.name for a in authors],
                "year": row.published_year,
                "publisher": row.publisher,
                "pages": row.page_count,
                "language": row.language,
                "rating": _number(row.goodreads_average_rating),
                "subjects": subjects[:MAX_SUBJECTS],
                "series": [
                    compact(
                        {
                            "name": member.name,
                            "position": _number(member.position),
                            "confirmed": member.confirmed,
                        }
                    )
                    for member in series
                ],
            }
        )
        if len(subjects) > MAX_SUBJECTS:
            record["subjects_omitted"] = len(subjects) - MAX_SUBJECTS
        return record

    async def get_series(self, name: str | None = None, id: int | None = None) -> dict[str, Any]:  # noqa: A002
        """A series in reading order. See descriptions.GET_SERIES."""
        if name is None and id is None:
            return {"error": "Provide either name or id."}

        async with self._engine.connect() as connection:
            if id is not None:
                row = await series_repo.get_series(connection, series_id=id)
                sought = str(id)
            else:
                row = await series_repo.find_series_by_name(connection, name=name)  # type: ignore[arg-type]
                sought = repr(name)

            if row is None:
                return {
                    "error": (
                        f"No series matching {sought}. Try search_books with the series "
                        "name to find books that belong to one."
                    )
                }

            members = await series_repo.members_of(connection, series_id=row.id)
            authors = await book_repo.authors_for(connection, [m.id for m in members])

        confirmed = sum(1 for member in members if member.confirmed)
        result: dict[str, Any] = {
            "id": row.id,
            "name": row.name,
            "books": [
                compact(
                    {
                        "id": member.id,
                        "isbn13": member.isbn13,
                        "title": member.title,
                        "authors": [a.name for a in authors.get(member.id, [])],
                        "position": _number(member.position),
                        "confirmed": member.confirmed,
                        "year": member.published_year,
                    }
                )
                for member in members
            ],
        }
        if members and confirmed < len(members):
            # Stated rather than left to be inferred from per-book flags: the
            # caller is about to present a reading order.
            result["note"] = (
                f"{len(members) - confirmed} of {len(members)} positions were inferred "
                "from titles rather than stated by a source; the order is a reasonable "
                "guess, not an established fact."
            )
        return result

    async def get_book_provenance(
        self,
        isbn13: str | None = None,
        id: int | None = None,  # noqa: A002
    ) -> dict[str, Any]:
        """Where a book's data came from. See descriptions.GET_BOOK_PROVENANCE."""
        async with self._engine.connect() as connection:
            if isbn13 is not None:
                normalised = normalise_isbn13(isbn13)
                if normalised is None:
                    return {"error": f"{isbn13!r} is not a valid ISBN-13."}
                row = await book_repo.get_book(connection, isbn13=normalised)
            elif id is not None:
                row = await book_repo.get_book_by_id(connection, book_id=id)
            else:
                return {"error": "Provide either isbn13 or id."}

            if row is None:
                return {"error": "No such book. Use search_books to find its identifier."}

            sources = await provenance_repo.sources_for(connection, book_id=row.id)

        return {
            "book": {"id": row.id, "title": row.title, "isbn13": row.isbn13},
            "sources": [
                compact(
                    {
                        "source": source.source,
                        "source_id": source.source_id,
                        "first_seen": source.first_seen_at.isoformat()
                        if source.first_seen_at
                        else None,
                        "last_seen": source.last_seen_at.isoformat()
                        if source.last_seen_at
                        else None,
                    }
                )
                for source in sources
            ],
            "disagreements": find_disagreements(sources, row),
        }

    async def list_contested_books(self, limit: int = 10) -> dict[str, Any]:
        """Books whose sources disagree. See descriptions.LIST_CONTESTED_BOOKS."""
        capped = max(1, min(limit, self._settings.mcp_max_results))

        async with self._engine.connect() as connection:
            candidates = await contested_repo.multi_source_books(connection, limit=200)
            payloads = await contested_repo.payloads_for(connection, [row.id for row in candidates])

        scored: list[dict[str, Any]] = []
        for row in candidates:
            conflicts = find_disagreements(payloads.get(row.id, []), row)
            if not conflicts:
                continue
            scored.append(
                compact(
                    {
                        "id": row.id,
                        "isbn13": row.isbn13,
                        "title": row.title,
                        "sources": row.sources,
                        "conflicting_fields": [c["field"] for c in conflicts],
                        "conflict_count": len(conflicts),
                    }
                )
            )

        # Most-contested first: the list is for triage, so the order is the
        # answer as much as the contents are.
        scored.sort(key=lambda item: item["conflict_count"], reverse=True)

        return {
            "contested": scored[:capped],
            "shown": len(scored[:capped]),
            "total_contested": len(scored),
            "note": (
                "Only books reported on by more than one source can appear here. "
                "A single-source book is unanimous because nothing has contradicted "
                "it, which is not the same as being corroborated."
            ),
        }

    async def describe_schema(self) -> dict[str, Any]:
        """The queryable schema. See descriptions.DESCRIBE_SCHEMA."""
        async with self._engine.connect() as connection:
            tables = await introspection_repo.describe(connection)

        return {
            "tables": tables,
            "note": (
                "Only these tables are queryable. Row counts are planner "
                "estimates, not exact — precise counts would cost a scan of "
                "every table to answer a question about shape."
            ),
        }

    async def run_sql(self, query: str) -> dict[str, Any]:
        """A bounded read-only query. See descriptions.RUN_SQL."""
        try:
            async with self._engine.connect() as connection:
                result = await introspection_repo.run_query(
                    connection, query, readonly_role=self._settings.sql_readonly_role or None
                )
        except introspection_repo.QueryRejectedError as rejection:
            # The rule and the remedy, not just a refusal: "invalid query" ends
            # the agent's turn, naming the constraint continues it.
            return {"error": str(rejection)}
        except Exception as error:
            return {
                "error": (
                    f"The query failed: {type(error).__name__}. Check column names "
                    "with describe_schema."
                )
            }

        payload: dict[str, Any] = {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
        }
        if result.truncated:
            payload["truncated"] = True
            payload["note"] = (
                f"Results were capped at {introspection_repo.MAX_ROWS} rows. Add a "
                "GROUP BY or a narrower WHERE clause rather than paging."
            )
        return payload

    async def catalogue_stats(self) -> dict[str, Any]:
        """What is in the catalogue. See descriptions.CATALOGUE_STATS."""
        async with self._engine.connect() as connection:
            counts = await stats_repo.coverage(connection)
            relationships = await stats_repo.relationship_counts(connection)
            sources = await stats_repo.per_source(connection)
            last_run = await stats_repo.last_successful_run(connection)

        total = counts.books

        def pct(value: int) -> float:
            return round(value / total * 100, 1) if total else 0.0

        return compact(
            {
                "books": total,
                "authors": relationships.authors,
                "subjects": relationships.subjects,
                "series": relationships.series,
                "coverage_percent": {
                    "isbn13": pct(counts.with_isbn),
                    "published_year": pct(counts.with_year),
                    "publisher": pct(counts.with_publisher),
                    "page_count": pct(counts.with_page_count),
                    "rating": pct(counts.with_rating),
                },
                "year_range": [counts.earliest_year, counts.latest_year]
                if counts.earliest_year
                else None,
                "sources": {row.source: row.books for row in sources},
                "last_updated": last_run.started_at.isoformat() if last_run else None,
                "note": (
                    "A missing field means no source supplied it, not that the value "
                    "is zero. Source counts overlap: a book resolved by three sources "
                    "appears under all three."
                ),
            }
        )


# Fields worth comparing across sources. Each maps to the keys a source's raw
# payload uses; sources spell the same fact differently, which is half the
# reason disagreement is worth surfacing at all.
_COMPARABLE = {
    "title": ("title",),
    "published_year": ("published", "first_publish_year", "publish_date", "publishedDate"),
    "publisher": ("publisher", "publishers"),
    "page_count": ("page_count", "number_of_pages", "number_of_pages_median", "pageCount"),
}

# Payload roots to search, in order. Google Books nests every field under
# volumeInfo, so a flat lookup finds nothing and reports agreement — which is
# worse than reporting an error, because a confident "the sources agree" is
# indistinguishable from a real one.
_PAYLOAD_ROOTS = ("", "volumeInfo")


def _readable(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten the roots a source might nest its fields under.

    Outer keys win: a field present at the top level is the source's own
    spelling, not a nested copy.
    """
    merged: dict[str, Any] = {}
    for root in reversed(_PAYLOAD_ROOTS):
        section = payload if root == "" else payload.get(root)
        if isinstance(section, dict):
            merged.update(section)
    return merged


def find_disagreements(sources: list[Any], book: Any) -> list[dict[str, Any]]:
    """Where sources reported different values for the same field.

    Compared against each source's *raw payload*, not the merged record — the
    merged record agrees with itself by construction, so comparing it would
    report harmony that was never there.

    Reported rather than resolved. One of these sources is an unofficial scrape
    that is demonstrably wrong about at least one ISBN, so a disagreement is
    information the caller should see, not an error to hide.
    """
    disagreements: list[dict[str, Any]] = []

    for field, keys in _COMPARABLE.items():
        reported: dict[str, str] = {}
        for source in sources:
            raw = source.raw_payload or {}
            if not isinstance(raw, dict):
                continue
            payload = _readable(raw)
            for key in keys:
                value = payload.get(key)
                if value in (None, "", []):
                    continue
                if isinstance(value, list):
                    value = value[0] if value else None
                if value is not None:
                    reported[source.source] = str(value).strip()
                break

        distinct = {value.lower() for value in reported.values()}
        if len(distinct) > 1:
            kept = getattr(book, field, None)
            disagreements.append(
                {
                    "field": field,
                    "reported": reported,
                    "kept": str(kept) if kept is not None else None,
                }
            )

    return disagreements
