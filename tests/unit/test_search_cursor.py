"""Search cursors.

They carry more than a position because relevance is a property of the query,
not of the row: a score, the mode that produced it, and a hash of the query
itself.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest

from api.pagination import InvalidCursorError
from api.search_cursor import (
    SEARCH_CURSOR_VERSION,
    SearchCursor,
    SearchMode,
    decode_search_cursor,
    query_fingerprint,
)


def cursor(**overrides: object) -> SearchCursor:
    base: dict[str, object] = {
        "mode": SearchMode.FULLTEXT,
        "score": Decimal("0.12345678"),
        "book_id": 42,
        "query_hash": query_fingerprint("dune"),
    }
    return SearchCursor(**{**base, **overrides})  # type: ignore[arg-type]


class TestRoundTrip:
    def test_a_cursor_survives_encoding(self) -> None:
        original = cursor()

        assert decode_search_cursor(original.encode(), query="dune") == original

    def test_the_score_keeps_its_precision(self) -> None:
        """The reason it is carried as a string.

        ts_rank returns `real`; a score that shifts in its last bits names a
        row that does not exist, and the page boundary skips or repeats.
        """
        original = cursor(score=Decimal("0.00000001"))

        assert decode_search_cursor(original.encode(), query="dune").score == Decimal("0.00000001")

    def test_the_score_never_becomes_a_float(self) -> None:
        decoded = decode_search_cursor(cursor().encode(), query="dune")

        assert isinstance(decoded.score, Decimal)

    def test_the_mode_survives(self) -> None:
        # Full-text and trigram scores are on different scales; forgetting
        # which produced the cursor compares one against the other.
        original = cursor(mode=SearchMode.SIMILARITY)

        assert decode_search_cursor(original.encode(), query="dune").mode is SearchMode.SIMILARITY


class TestQueryBinding:
    def test_a_cursor_from_another_query_is_refused(self) -> None:
        """Its scores rank a different result set."""
        issued = cursor()

        with pytest.raises(InvalidCursorError, match="different query"):
            decode_search_cursor(issued.encode(), query="foundation")

    def test_the_same_query_differently_cased_is_accepted(self) -> None:
        # Normalised before hashing: "Dune" and "dune" are one query.
        issued = cursor()

        assert decode_search_cursor(issued.encode(), query="  DUNE ")

    def test_the_fingerprint_is_stable(self) -> None:
        assert query_fingerprint("dune") == query_fingerprint("Dune ")

    def test_different_queries_fingerprint_differently(self) -> None:
        assert query_fingerprint("dune") != query_fingerprint("foundation")


class TestRefusal:
    def _payload(self, **fields: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(fields).encode()).decode().rstrip("=")

    @pytest.mark.parametrize("raw", ["", "!!!", "eyJhIjogMX0"], ids=["empty", "junk", "wrong"])
    def test_a_corrupt_cursor_is_refused(self, raw: str) -> None:
        with pytest.raises(InvalidCursorError):
            decode_search_cursor(raw, query="dune")

    def test_a_json_array_is_refused(self) -> None:
        raw = base64.urlsafe_b64encode(json.dumps([1, 2]).encode()).decode().rstrip("=")

        with pytest.raises(InvalidCursorError, match="object"):
            decode_search_cursor(raw, query="dune")

    def test_an_older_version_is_refused(self) -> None:
        raw = self._payload(v=SEARCH_CURSOR_VERSION - 1, m="fulltext", s="0.1", i=1, q="x")

        with pytest.raises(InvalidCursorError, match="version"):
            decode_search_cursor(raw, query="dune")

    def test_missing_fields_are_named(self) -> None:
        raw = self._payload(v=SEARCH_CURSOR_VERSION, m="fulltext")

        with pytest.raises(InvalidCursorError, match="missing"):
            decode_search_cursor(raw, query="dune")

    def test_an_unknown_mode_is_refused(self) -> None:
        raw = self._payload(
            v=SEARCH_CURSOR_VERSION, m="magic", s="0.1", i=1, q=query_fingerprint("dune")
        )

        with pytest.raises(InvalidCursorError, match="mode"):
            decode_search_cursor(raw, query="dune")

    def test_a_boolean_identifier_is_refused(self) -> None:
        # bool is an int subclass; `true` would become book id 1.
        raw = self._payload(
            v=SEARCH_CURSOR_VERSION, m="fulltext", s="0.1", i=True, q=query_fingerprint("dune")
        )

        with pytest.raises(InvalidCursorError, match="identifier"):
            decode_search_cursor(raw, query="dune")

    def test_an_unparseable_score_is_refused(self) -> None:
        raw = self._payload(
            v=SEARCH_CURSOR_VERSION, m="fulltext", s="high", i=1, q=query_fingerprint("dune")
        )

        with pytest.raises(InvalidCursorError, match="score"):
            decode_search_cursor(raw, query="dune")
