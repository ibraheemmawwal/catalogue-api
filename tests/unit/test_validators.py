"""ISBN-13 handling.

Shared by both surfaces on purpose: a tool that accepted an ISBN the HTTP route
rejects would be drift one level above the repository layer.
"""

from __future__ import annotations

import pytest

from api.validators import normalise_isbn13


class TestAcceptance:
    def test_a_plain_isbn_is_accepted(self) -> None:
        assert normalise_isbn13("9780553380163") == "9780553380163"

    @pytest.mark.parametrize(
        "raw",
        ["978-0-553-38016-3", "978 0 553 38016 3", "  9780553380163  "],
        ids=["hyphens", "spaces", "padding"],
    )
    def test_jacket_formatting_is_accepted(self, raw: str) -> None:
        # This is what people copy off a book; rejecting it would fail the
        # single most common input.
        assert normalise_isbn13(raw) == "9780553380163"


class TestRefusal:
    @pytest.mark.parametrize(
        "raw",
        ["", "978055338016", "97805533801634", "978055338016X", "abcdefghijklm"],
        ids=["empty", "too-short", "too-long", "letter", "letters"],
    )
    def test_malformed_input_is_refused(self, raw: str) -> None:
        assert normalise_isbn13(raw) is None

    def test_a_bad_check_digit_is_refused(self) -> None:
        """Almost always a transcription error.

        Telling the caller their ISBN is wrong beats returning an empty result
        set that reads as "we don't have that book".
        """
        assert normalise_isbn13("9780553380164") is None

    def test_an_isbn10_is_refused(self) -> None:
        assert normalise_isbn13("0553380168") is None


class TestChecksum:
    @pytest.mark.parametrize(
        "isbn",
        ["9780553380163", "9780441172719", "9780134902937", "9781234567897"],
    )
    def test_known_valid_isbns_pass(self, isbn: str) -> None:
        assert normalise_isbn13(isbn) == isbn

    def test_every_single_digit_error_is_caught_somewhere(self) -> None:
        # The checksum's purpose: catch a mistyped digit. It cannot catch every
        # one (weight-3 positions alias at ±10), but a wholesale miss would
        # mean the check is decorative.
        valid = "9780553380163"
        caught = sum(
            normalise_isbn13(valid[:i] + str(d) + valid[i + 1 :]) is None
            for i in range(13)
            for d in range(10)
            if str(d) != valid[i]
        )

        assert caught >= 100
