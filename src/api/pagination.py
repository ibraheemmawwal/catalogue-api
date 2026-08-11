"""Keyset pagination.

Offset pagination is wrong for this collection, not merely slower: the pipeline
writes to `books` while the API reads it, and under `OFFSET n` a row inserted
earlier in the sort order shifts everything after it — so a client paging
through can see a book twice or miss one entirely, with no error either time.

The key is `(lower(title), id)`. Title alone is not unique; adding the primary
key makes the ordering total, which is what lets a cursor name exactly one row.
Sorting on the publication year would have been the obvious alternative and is
unusable here — a third of the catalogue has no year, and NULLs cannot anchor a
cursor.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

# Bumped whenever the payload shape changes. Without it, an old cursor from a
# client's saved link decodes as garbage under the new reader and produces a
# wrong page rather than an honest error.
CURSOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class Cursor:
    """The position of the last row on the previous page."""

    sort_title: str
    book_id: int

    def encode(self) -> str:
        """URL-safe Base64 of a compact JSON payload.

        Opaque on purpose: a client that parses the cursor is a client we can
        never change the sort key for.
        """
        payload = json.dumps(
            {"v": CURSOR_VERSION, "t": self.sort_title, "i": self.book_id},
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


class InvalidCursorError(ValueError):
    """A cursor that cannot be trusted to name a row."""


def decode_cursor(raw: str) -> Cursor:
    """Decode a cursor, or refuse it.

    Every failure is the same class of event — the cursor did not come from us
    intact — and all of them must refuse rather than guess. A cursor decoded
    into the wrong position silently returns the wrong page.
    """
    try:
        # Padding is stripped on encode to keep URLs clean; restore it here.
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        msg = "cursor is not valid Base64-encoded JSON"
        raise InvalidCursorError(msg) from error

    if not isinstance(payload, dict):
        msg = "cursor payload is not an object"
        raise InvalidCursorError(msg)

    version = payload.get("v")
    if version != CURSOR_VERSION:
        # Named explicitly: this is the one failure a caller can act on by
        # restarting pagination rather than by fixing their code.
        msg = f"cursor version {version!r} is not supported (expected {CURSOR_VERSION})"
        raise InvalidCursorError(msg)

    # Absence and malformation are reported apart on purpose: one says the
    # cursor came from an older shape, the other that it was corrupted in
    # transit, and an operator reading a log wants to know which.
    if "t" not in payload or "i" not in payload:
        msg = "cursor is missing its position fields"
        raise InvalidCursorError(msg)

    title, identifier = payload["t"], payload["i"]
    if not isinstance(title, str):
        msg = "cursor does not carry a valid sort position"
        raise InvalidCursorError(msg)
    # bool is an int subclass; a JSON `true` would otherwise become book id 1
    # and silently return the wrong page.
    if not isinstance(identifier, int) or isinstance(identifier, bool):
        msg = "cursor does not carry a valid book identifier"
        raise InvalidCursorError(msg)

    return Cursor(sort_title=title, book_id=identifier)


@dataclass(frozen=True, slots=True)
class Page:
    """One page of results, and whether there is another."""

    items: list[Any]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


def build_page(rows: list[Any], limit: int, *, cursor_of: Any) -> Page:
    """Trim an over-fetched result set into a page.

    The repository asks for ``limit + 1`` rows. Whether that extra row came
    back is the only reliable "is there more" signal — counting the matches
    instead would mean a second aggregate query per request, on a filter the
    planner has already walked.
    """
    if len(rows) <= limit:
        return Page(items=rows, next_cursor=None)

    kept = rows[:limit]
    return Page(items=kept, next_cursor=cursor_of(kept[-1]).encode())
