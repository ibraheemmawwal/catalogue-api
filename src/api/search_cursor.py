"""The search cursor.

Distinct from the title cursor because search pages are ordered by relevance,
and relevance is a property of the query rather than of the row. Three
consequences the shape has to carry:

*Score.* Ordering is ``(rank DESC, id DESC)``, so a cursor must name both.

*Mode.* Full-text and trigram-similarity searches produce scores on different
scales. A cursor that forgot which one produced it would compare a trigram
score against a ts_rank and page somewhere arbitrary.

*Query hash.* A cursor from one query used against another is meaningless —
the scores describe a different ranking. Hashing the query lets that be
refused rather than silently answered.

The score is carried as a decimal string, never a float. ``ts_rank`` returns
``real``; round-tripping it through JSON and Python floats changes the value in
the last bits, and a keyset comparison against a changed value skips or repeats
rows at every page boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from api.pagination import InvalidCursorError

SEARCH_CURSOR_VERSION = 1

# Matching the rounding the query applies before ordering. If these disagreed,
# the cursor would name a score no row actually has.
SCORE_PRECISION = 8


class SearchMode(StrEnum):
    """How the current result set was ranked."""

    FULLTEXT = "fulltext"
    SIMILARITY = "similarity"


def query_fingerprint(query: str) -> str:
    """A short, stable hash of the normalised query.

    Only long enough to catch a cursor being reused against a different query;
    this is a correctness guard, not a security boundary.
    """
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SearchCursor:
    """Where the previous page of a search ended."""

    mode: SearchMode
    score: Decimal
    book_id: int
    query_hash: str

    def encode(self) -> str:
        payload = json.dumps(
            {
                "v": SEARCH_CURSOR_VERSION,
                "m": self.mode.value,
                # A string, deliberately: json.dumps of a Decimal would either
                # fail or become a float, and a float score breaks the keyset.
                "s": format(self.score, f".{SCORE_PRECISION}f"),
                "i": self.book_id,
                "q": self.query_hash,
            },
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_search_cursor(raw: str, *, query: str) -> SearchCursor:
    """Decode a search cursor, or refuse it.

    Raises:
        InvalidCursorError: malformed, from another version, or from another
            query.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError) as error:
        msg = "search cursor is not valid Base64-encoded JSON"
        raise InvalidCursorError(msg) from error

    if not isinstance(payload, dict):
        msg = "search cursor payload is not an object"
        raise InvalidCursorError(msg)

    if payload.get("v") != SEARCH_CURSOR_VERSION:
        msg = f"search cursor version {payload.get('v')!r} is not supported"
        raise InvalidCursorError(msg)

    missing = {"m", "s", "i", "q"} - set(payload)
    if missing:
        msg = f"search cursor is missing {', '.join(sorted(missing))}"
        raise InvalidCursorError(msg)

    if payload["q"] != query_fingerprint(query):
        # The scores in this cursor rank a different result set; continuing
        # would return a page that belongs to neither query.
        msg = "search cursor was issued for a different query; start again without a cursor"
        raise InvalidCursorError(msg)

    try:
        mode = SearchMode(payload["m"])
    except ValueError as error:
        msg = f"search cursor names an unknown mode {payload['m']!r}"
        raise InvalidCursorError(msg) from error

    identifier = payload["i"]
    if not isinstance(identifier, int) or isinstance(identifier, bool):
        msg = "search cursor does not carry a valid book identifier"
        raise InvalidCursorError(msg)

    try:
        # str() first: a JSON number here would already have lost precision,
        # and Decimal(float) would preserve the loss rather than the value.
        score = Decimal(str(payload["s"]))
    except (InvalidOperation, TypeError) as error:
        msg = "search cursor does not carry a valid score"
        raise InvalidCursorError(msg) from error

    return SearchCursor(mode=mode, score=score, book_id=identifier, query_hash=payload["q"])
