"""Tool descriptions.

Kept apart from the functions on purpose. These are prompt surface: they are
the entire basis on which a model chooses a tool, they ride in every request
that has the toolset attached, and they get re-tuned as models change. Buried
in docstrings, that review is harder than it needs to be.

Two rules they follow:

**Say when to call it, not just what it does.** A description that states its
trigger condition measurably improves tool selection. "Returns provenance
records" describes; "call this when the user asks where data came from"
triggers.

**Do not shout.** `CRITICAL: You MUST call this` was a workaround for models
that under-triggered. On current models it causes over-triggering, which is a
worse failure because it is quieter.

No worked examples and no embedded workflows live here — both cost tokens on
every request. They belong in the server instructions, which is sent once.
"""

from __future__ import annotations

SERVER_INSTRUCTIONS = """\
This is a read-only catalogue of books assembled from four sources: Goodreads,
Open Library, Google Books and Project Gutenberg.

What makes it unusual is that it remembers where every fact came from. If a
question touches reliability — who says so, is this confirmed, do sources
disagree — reach for get_book_provenance rather than answering from the merged
record alone.

Coverage is uneven and worth checking before drawing conclusions from absence:
roughly a third of books have no publication year, and a missing field means
no source supplied it, not that the value is zero or unknown-and-unknowable.
Call catalogue_stats when a question depends on how complete the data is.

Series positions carry a `confirmed` flag. A confirmed position was stated by a
source; an unconfirmed one was inferred from the book's title. Say which when
presenting a reading order.
"""

SEARCH_BOOKS = """\
Find books by title, author, subject, series or publication year.

Call this whenever the user names a book, author or series and you need its
identifier, or when they describe the kind of book they are looking for. Start
here rather than guessing an ISBN.

Returns compact records — identifier, title, authors, year — plus the total
number of matches. When there are more matches than results, narrow the query
with the filters rather than asking for a larger page: the total tells you how
much narrowing is needed. Use get_book for the full record of a specific book.
"""

GET_BOOK = """\
Get everything the catalogue holds about one book: subtitle, publisher, page
count, subjects, series membership and rating.

Call this once you have an identifier from search_books and need detail beyond
title and author. Accepts either an ISBN-13 or the numeric id from a search
result — books that never had an ISBN are identified by id alone.
"""

GET_SERIES = """\
Get a series and its books in reading order.

Call this when the user asks what order to read something in, or what else is
in a series. Accepts a series name or its identifier.

Each member carries a `confirmed` flag: true when a source stated that
position, false when it was inferred from the title. Reading order built from
unconfirmed positions is a reasonable guess rather than an established fact,
and worth presenting that way.
"""

GET_BOOK_PROVENANCE = """\
Show which sources supplied a book, when they were last read, and where they
disagree about a field.

Call this when the user asks where information came from, how reliable it is,
whether it is up to date, or why two answers to the same question differ. Also
worth calling before presenting a fact the user will act on.

Disagreements are reported rather than resolved: each one names the field, what
each source said, and which value the catalogue kept. Sources are not equally
reliable — one is an unofficial scrape — so a disagreement is information, not
an error.
"""

CATALOGUE_STATS = """\
Report what is in the catalogue and how complete it is: totals, per-field
coverage percentages, which sources contributed, and when it last updated.

Call this when a question depends on whether the data can support an answer —
before aggregating, comparing across years, or concluding that something is
absent. A missing field means no source supplied it, which is different from
the value being known to be zero.
"""
