"""Cross-source disagreement detection.

The logic behind the tool this catalogue exists to make possible. Pure, so it
is tested directly: which values count as conflicting, and which spellings of
the same field count as the same field.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from api.mcp.tools import find_disagreements


def source(name: str, **payload: Any) -> Any:
    return SimpleNamespace(source=name, raw_payload=payload)


def book(**fields: Any) -> Any:
    return SimpleNamespace(**{"title": "Dune", "published_year": 1965, **fields})


class TestAgreement:
    def test_sources_that_agree_produce_nothing(self) -> None:
        found = find_disagreements(
            [source("goodreads", title="Dune"), source("openlibrary", title="Dune")], book()
        )

        assert found == []

    def test_a_single_source_cannot_disagree(self) -> None:
        assert find_disagreements([source("goodreads", title="Dune")], book()) == []

    def test_no_sources_produce_nothing(self) -> None:
        assert find_disagreements([], book()) == []

    def test_case_differences_are_not_disagreements(self) -> None:
        # "DUNE" and "Dune" are the same claim spelled differently.
        found = find_disagreements(
            [source("goodreads", title="DUNE"), source("openlibrary", title="Dune")], book()
        )

        assert found == []

    def test_a_source_that_reported_nothing_is_ignored(self) -> None:
        # Silence is not disagreement.
        found = find_disagreements(
            [source("goodreads", title="Dune"), source("openlibrary")], book()
        )

        assert found == []


class TestDisagreement:
    def test_conflicting_titles_are_reported(self) -> None:
        found = find_disagreements(
            [
                source("goodreads", title="Dune Study Guide"),
                source("openlibrary", title="Dune"),
            ],
            book(),
        )

        assert found[0]["field"] == "title"
        assert found[0]["reported"] == {
            "goodreads": "Dune Study Guide",
            "openlibrary": "Dune",
        }

    def test_the_kept_value_is_named(self) -> None:
        """Reported, not resolved.

        One of these sources is an unofficial scrape that is demonstrably wrong
        about at least one ISBN, so which value won is information the caller
        should be able to see.
        """
        found = find_disagreements(
            [source("goodreads", title="Wrong"), source("openlibrary", title="Dune")],
            book(title="Dune"),
        )

        assert found[0]["kept"] == "Dune"

    def test_sources_spelling_a_field_differently_still_compare(self) -> None:
        # Open Library says first_publish_year, Google Books says publishedDate.
        # Treating them as separate fields would hide every year conflict.
        found = find_disagreements(
            [
                source("openlibrary", first_publish_year=1965),
                source("googlebooks", publishedDate=1990),
            ],
            book(),
        )

        assert [f["field"] for f in found] == ["published_year"]

    def test_a_list_valued_field_compares_its_first_entry(self) -> None:
        found = find_disagreements(
            [
                source("openlibrary", publishers=["Ace"]),
                source("googlebooks", publisher="Chilton"),
            ],
            book(publisher="Ace"),
        )

        assert [f["field"] for f in found] == ["publisher"]

    def test_several_fields_can_disagree_at_once(self) -> None:
        found = find_disagreements(
            [
                source("goodreads", title="A", page_count=100),
                source("openlibrary", title="B", number_of_pages=200),
            ],
            book(),
        )

        assert {f["field"] for f in found} == {"title", "page_count"}


class TestRobustness:
    def test_a_non_dict_payload_is_skipped(self) -> None:
        # raw_payload is JSONB; nothing guarantees an object at this layer.
        found = find_disagreements(
            [SimpleNamespace(source="odd", raw_payload=["not", "a", "dict"])], book()
        )

        assert found == []

    def test_a_null_payload_is_skipped(self) -> None:
        found = find_disagreements([SimpleNamespace(source="odd", raw_payload=None)], book())

        assert found == []

    def test_empty_values_do_not_count_as_reports(self) -> None:
        found = find_disagreements(
            [source("goodreads", title=""), source("openlibrary", title="Dune")], book()
        )

        assert found == []


class TestNestedPayloads:
    """Sources that nest their fields.

    Google Books puts everything under volumeInfo. A flat lookup finds nothing
    and reports agreement — the worst outcome available, because a confident
    "the sources agree" is indistinguishable from a real one.
    """

    def test_a_nested_field_is_compared(self) -> None:
        found = find_disagreements(
            [
                source("openlibrary", first_publish_year=1965),
                SimpleNamespace(
                    source="googlebooks",
                    raw_payload={"volumeInfo": {"publishedDate": "1990-09-01"}},
                ),
            ],
            book(),
        )

        assert [f["field"] for f in found] == ["published_year"]

    def test_a_nested_field_can_also_agree(self) -> None:
        found = find_disagreements(
            [
                source("openlibrary", title="Dune"),
                SimpleNamespace(
                    source="googlebooks", raw_payload={"volumeInfo": {"title": "Dune"}}
                ),
            ],
            book(),
        )

        assert found == []

    def test_a_top_level_field_wins_over_a_nested_one(self) -> None:
        # The source's own spelling, not a nested copy of it.
        found = find_disagreements(
            [
                SimpleNamespace(
                    source="odd",
                    raw_payload={"title": "Outer", "volumeInfo": {"title": "Inner"}},
                ),
                source("openlibrary", title="Outer"),
            ],
            book(),
        )

        assert found == []

    def test_page_counts_are_compared_across_spellings(self) -> None:
        # OL says number_of_pages_median, Google Books says pageCount.
        found = find_disagreements(
            [
                source("openlibrary", number_of_pages_median=412),
                SimpleNamespace(
                    source="googlebooks", raw_payload={"volumeInfo": {"pageCount": 896}}
                ),
            ],
            book(),
        )

        assert [f["field"] for f in found] == ["page_count"]
