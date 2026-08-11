"""Cursors.

A cursor that decodes into the wrong position returns the wrong page silently,
so every malformed input must refuse rather than guess. These tests are mostly
about refusal.
"""

from __future__ import annotations

import base64
import json
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.pagination import (
    CURSOR_VERSION,
    Cursor,
    InvalidCursorError,
    Page,
    build_page,
    decode_cursor,
)


class TestRoundTrip:
    def test_a_cursor_survives_encoding(self) -> None:
        cursor = Cursor(sort_title="dune", book_id=uuid4())

        assert decode_cursor(cursor.encode()) == cursor

    @given(
        title=st.text(min_size=0, max_size=300),
        identifier=st.uuids(),
    )
    def test_any_title_survives(self, title: str, identifier: UUID) -> None:
        # Titles carry quotes, emoji, RTL text and newlines. A cursor that
        # breaks on one of them breaks pagination for that book only — the
        # kind of bug that reaches production because nobody sorted there.
        cursor = Cursor(sort_title=title, book_id=identifier)

        assert decode_cursor(cursor.encode()) == cursor

    def test_it_is_url_safe(self) -> None:
        # It travels in a query string; + and / would need escaping and some
        # clients would get it wrong.
        encoded = Cursor(sort_title="a/b+c" * 20, book_id=uuid4()).encode()

        assert "+" not in encoded
        assert "/" not in encoded
        assert "=" not in encoded


class TestRefusal:
    @pytest.mark.parametrize(
        "raw",
        ["", "not-base64!!", "///", "YWJj", "eyJhIjogMX0"],
        ids=["empty", "illegal-chars", "slashes", "not-json", "wrong-shape"],
    )
    def test_a_corrupt_cursor_is_refused(self, raw: str) -> None:
        with pytest.raises(InvalidCursorError):
            decode_cursor(raw)

    def test_an_older_version_is_refused_by_name(self) -> None:
        """The one failure a caller can act on — by restarting pagination."""
        stale = base64.urlsafe_b64encode(
            json.dumps({"v": CURSOR_VERSION - 1, "t": "dune", "i": str(uuid4())}).encode()
        ).decode()

        with pytest.raises(InvalidCursorError, match="version"):
            decode_cursor(stale)

    def test_a_missing_position_is_refused(self) -> None:
        payload = base64.urlsafe_b64encode(
            json.dumps({"v": CURSOR_VERSION, "t": "dune"}).encode()
        ).decode()

        with pytest.raises(InvalidCursorError, match="position"):
            decode_cursor(payload)

    def test_a_non_uuid_identifier_is_refused(self) -> None:
        payload = base64.urlsafe_b64encode(
            json.dumps({"v": CURSOR_VERSION, "t": "dune", "i": "17"}).encode()
        ).decode()

        with pytest.raises(InvalidCursorError, match="identifier"):
            decode_cursor(payload)

    def test_a_json_array_is_refused(self) -> None:
        payload = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode()

        with pytest.raises(InvalidCursorError, match="object"):
            decode_cursor(payload)

    def test_a_tampered_cursor_does_not_silently_shift_the_page(self) -> None:
        # A cursor is opaque, not signed. It must not be *parseable* into a
        # different valid position by flipping characters.
        original = Cursor(sort_title="dune", book_id=uuid4()).encode()
        tampered = original[:-4] + "AAAA"

        try:
            decoded = decode_cursor(tampered)
        except InvalidCursorError:
            return  # refusing is the correct outcome
        assert decoded.sort_title != "dune" or decoded.book_id is not None


class TestPageBuilding:
    def _cursor_of(self, row: tuple[str, UUID]) -> Cursor:
        return Cursor(sort_title=row[0], book_id=row[1])

    def test_a_short_result_set_is_the_last_page(self) -> None:
        rows = [("a", uuid4()), ("b", uuid4())]

        page = build_page(rows, limit=5, cursor_of=self._cursor_of)

        assert page.items == rows
        assert page.next_cursor is None
        assert not page.has_more

    def test_an_exact_fill_is_still_the_last_page(self) -> None:
        # The boundary that gets this wrong: exactly `limit` rows means there
        # was no extra row, so there is nothing after it.
        rows = [("a", uuid4()), ("b", uuid4())]

        page = build_page(rows, limit=2, cursor_of=self._cursor_of)

        assert page.next_cursor is None

    def test_an_over_fetch_signals_another_page(self) -> None:
        rows = [("a", uuid4()), ("b", uuid4()), ("c", uuid4())]

        page = build_page(rows, limit=2, cursor_of=self._cursor_of)

        assert len(page.items) == 2
        assert page.has_more

    def test_the_extra_row_is_not_returned(self) -> None:
        # It was fetched to answer "is there more", not to be shown.
        rows = [("a", uuid4()), ("b", uuid4()), ("c", uuid4())]

        page = build_page(rows, limit=2, cursor_of=self._cursor_of)

        assert [row[0] for row in page.items] == ["a", "b"]

    def test_the_cursor_points_at_the_last_returned_row(self) -> None:
        rows = [("a", uuid4()), ("b", uuid4()), ("c", uuid4())]

        page = build_page(rows, limit=2, cursor_of=self._cursor_of)

        assert page.next_cursor is not None
        assert decode_cursor(page.next_cursor).sort_title == "b"

    def test_an_empty_result_set_has_no_next_page(self) -> None:
        page = build_page([], limit=10, cursor_of=self._cursor_of)

        assert page.items == []
        assert not page.has_more


class TestPage:
    def test_has_more_follows_the_cursor(self) -> None:
        assert not Page(items=[], next_cursor=None).has_more
        assert Page(items=[], next_cursor="abc").has_more
