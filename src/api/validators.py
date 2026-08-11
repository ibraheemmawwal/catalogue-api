"""Input validation shared by both surfaces.

Lives outside the routers so the MCP tools apply exactly the same rules. A
tool that accepted an ISBN the HTTP route rejects would be the same drift the
shared repository layer exists to prevent, one level up.
"""

from __future__ import annotations

import re

_ISBN13 = re.compile(r"^\d{13}$")


def normalise_isbn13(raw: str) -> str | None:
    """Strip separators and validate, or return None.

    Hyphenated ISBNs are what people copy from a book jacket, so accepting them
    is not leniency — rejecting them would fail the most common input.

    The check digit is verified because an ISBN that passes the format check
    but fails the checksum is almost always a transcription error, and telling
    the caller that is more useful than an empty result set.
    """
    candidate = raw.replace("-", "").replace(" ", "").strip()
    if not _ISBN13.match(candidate):
        return None

    # ISBN-13 checksum: alternating weights of 1 and 3, total divisible by 10.
    total = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(candidate))
    return candidate if total % 10 == 0 else None
