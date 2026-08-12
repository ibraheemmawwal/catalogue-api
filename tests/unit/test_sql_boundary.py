"""The boundary on caller-supplied SQL.

This is the only place in the service where a caller supplies a query rather
than choosing a tool, and the endpoint is unauthenticated. Every rule below
exists for that reason, and these tests are adversarial on purpose: the ones
that matter are the queries that *look* like reads.
"""

from __future__ import annotations

import contextlib

import pytest

from api.repositories.introspection import (
    MAX_ROWS,
    QUERYABLE_TABLES,
    QueryRejectedError,
    validate,
)


class TestOnlyReads:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO books (title) VALUES ('x')",
            "UPDATE books SET title = 'x'",
            "DELETE FROM books",
            "DROP TABLE books",
            "TRUNCATE books",
            "ALTER TABLE books ADD COLUMN x int",
            "GRANT ALL ON books TO PUBLIC",
        ],
    )
    def test_writes_are_refused(self, sql: str) -> None:
        with pytest.raises(QueryRejectedError):
            validate(sql)

    def test_a_write_hidden_after_a_read_is_refused(self) -> None:
        """The case every other rule would let through.

        "SELECT 1; DROP TABLE books" starts with a perfectly good read, so a
        prefix check passes it. Only the single-statement rule catches it.
        """
        with pytest.raises(QueryRejectedError, match="one statement"):
            validate("SELECT 1; DROP TABLE books")

    def test_a_trailing_semicolon_is_fine(self) -> None:
        # Common and harmless; refusing it would train a caller to strip
        # punctuation rather than to stop chaining statements.
        assert validate("SELECT title FROM books;")

    def test_a_cte_is_allowed(self) -> None:
        sql = "WITH recent AS (SELECT * FROM books) SELECT count(*) FROM recent"

        assert validate(sql)

    def test_a_cte_that_writes_is_refused(self) -> None:
        # PostgreSQL genuinely permits DML inside a CTE, so "starts with WITH"
        # is not sufficient on its own.
        sql = "WITH gone AS (DELETE FROM books RETURNING id) SELECT count(*) FROM gone"

        with pytest.raises(QueryRejectedError):
            validate(sql)


class TestTableAllowlist:
    def test_catalogue_tables_are_allowed(self) -> None:
        assert validate("SELECT title FROM books")

    @pytest.mark.parametrize("table", ["pg_authid", "pg_shadow", "pg_user", "alembic_version"])
    def test_other_tables_are_refused(self, table: str) -> None:
        # An allowlist, not a denylist: a denylist must anticipate every
        # catalogue and extension view, and it only has to be wrong once.
        with pytest.raises(QueryRejectedError, match="non-queryable"):
            validate(f"SELECT * FROM {table}")

    def test_the_refusal_names_what_is_allowed(self) -> None:
        # A model can act on the list; it cannot act on "denied".
        with pytest.raises(QueryRejectedError, match="books"):
            validate("SELECT * FROM pg_authid")

    def test_a_join_onto_a_forbidden_table_is_refused(self) -> None:
        sql = "SELECT b.title FROM books b JOIN pg_authid a ON true"

        with pytest.raises(QueryRejectedError):
            validate(sql)

    def test_a_cte_name_is_not_mistaken_for_a_table(self) -> None:
        # Names defined by the query itself are not tables; treating them as
        # unknown would reject most non-trivial queries.
        sql = "WITH ranked AS (SELECT id FROM books) SELECT * FROM ranked"

        assert validate(sql)

    def test_every_allowlisted_table_is_a_catalogue_table(self) -> None:
        assert "pg_authid" not in QUERYABLE_TABLES
        assert "alembic_version" not in QUERYABLE_TABLES


class TestDangerousFunctions:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_read_file('/etc/passwd')",
            "SELECT pg_sleep(60)",
            "SELECT lo_import('/etc/passwd')",
            "SELECT dblink('', 'SELECT 1')",
        ],
    )
    def test_they_are_refused(self, sql: str) -> None:
        # Each is a read by the letter of the rule and none is a read of this
        # catalogue.
        with pytest.raises(QueryRejectedError):
            validate(sql)


class TestLimits:
    def test_a_limit_is_added_when_absent(self) -> None:
        # So a caller does not have to know the rule to write a query that
        # returns something sensible.
        assert "LIMIT" in validate("SELECT title FROM books")

    def test_the_added_limit_asks_for_one_row_past_the_cap(self) -> None:
        """The spare row is how truncation is detected at all.

        Asking for exactly MAX_ROWS makes a query with 200 matches and one with
        200,000 return identical results, and the caller is told the capped
        page is the complete answer.
        """
        assert f"LIMIT {MAX_ROWS + 1}" in validate("SELECT title FROM books")

    def test_an_existing_limit_is_left_alone(self) -> None:
        result = validate("SELECT title FROM books LIMIT 5")

        assert result.count("LIMIT") == 1
        assert "LIMIT 5" in result


class TestRejectionMessages:
    def test_an_empty_query_says_what_to_send(self) -> None:
        with pytest.raises(QueryRejectedError, match="SELECT"):
            validate("   ")

    def test_a_write_says_the_catalogue_is_read_only(self) -> None:
        with pytest.raises(QueryRejectedError, match="read-only"):
            validate("UPDATE books SET title = 'x'")


class TestFalsePositives:
    """Rules that reject legitimate queries are their own failure.

    A boundary nobody can work inside gets worked around.
    """

    def test_a_column_containing_a_keyword_is_fine(self) -> None:
        # "updated_at" contains "update"; a substring check would reject it.
        assert validate("SELECT updated_at FROM books")

    def test_aggregates_are_fine(self) -> None:
        sql = (
            "SELECT published_year, count(*) FROM books "
            "GROUP BY published_year ORDER BY count(*) DESC"
        )

        assert validate(sql)

    def test_joins_across_catalogue_tables_are_fine(self) -> None:
        sql = (
            "SELECT a.name, count(*) FROM authors a "
            "JOIN book_authors ba ON ba.author_id = a.id GROUP BY a.name"
        )

        assert validate(sql)


class TestEvasions:
    """Ways past the table allowlist that a single regex did not catch.

    Every case here passed validation in the first implementation. They are
    kept as tests rather than fixed and forgotten because each one reaches a
    table the caller must not read, on an unauthenticated endpoint.
    """

    @pytest.mark.parametrize(
        ("evasion", "sql"),
        [
            ("quoted identifier", 'SELECT * FROM "pg_authid"'),
            ("comment before the name", "SELECT * FROM /*x*/ pg_authid"),
            ("line comment before the name", "SELECT * FROM --x\n pg_authid"),
            ("second entry in a comma list", "SELECT * FROM books, pg_authid"),
            ("comma list with aliases", "SELECT * FROM books b, pg_shadow s"),
            ("schema qualified", "SELECT * FROM pg_catalog.pg_authid"),
            ("quoted and qualified", 'SELECT * FROM "pg_catalog"."pg_authid"'),
            ("set-returning function", "SELECT * FROM pg_ls_waldir()"),
            ("join onto a system view", "SELECT * FROM books JOIN pg_settings ON true"),
        ],
    )
    def test_the_table_is_refused(self, evasion: str, sql: str) -> None:
        with pytest.raises(QueryRejectedError):
            validate(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT version()",
            "SELECT current_setting('data_directory')",
            "SELECT current_user",
        ],
    )
    def test_a_query_reading_no_table_is_refused(self, sql: str) -> None:
        # Not a catalogue question. Every real one reads a table, and the ones
        # that do not are asking the server about itself.
        with pytest.raises(QueryRejectedError, match="at least one catalogue table"):
            validate(sql)


class TestTheseStillWork:
    """The shapes an agent actually writes.

    Added alongside the evasion fixes: tightening a parser is how legitimate
    queries start getting refused, and a boundary nobody can work inside gets
    worked around.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT title FROM books",
            "SELECT * FROM public.books",
            "SELECT b.title FROM books b, book_authors ba WHERE ba.book_id = b.id",
            "SELECT * FROM (SELECT id FROM books) t",
            "WITH r AS (SELECT id FROM books) SELECT count(*) FROM r",
            "SELECT a.name FROM authors a JOIN book_authors ba ON ba.author_id = a.id",
            "SELECT a.name FROM authors AS a LEFT JOIN book_authors ba ON true",
            "SELECT title FROM books ORDER BY title LIMIT 10",
        ],
    )
    def test_it_is_allowed(self, sql: str) -> None:
        assert validate(sql)


class TestMalformedInput:
    """Half-written SQL must be refused, not crash the parser.

    An agent producing a truncated statement is ordinary; a tool that raises
    something other than a rejection turns that into a broken turn instead of
    a correctable error.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM",
            "SELECT * FROM ",
            "SELECT * FROM 123",
            "SELECT * FROM ,",
            "WITH",
        ],
    )
    def test_it_is_refused_rather_than_raising(self, sql: str) -> None:
        with pytest.raises(QueryRejectedError):
            validate(sql)

    def test_unbalanced_parentheses_do_not_hang(self) -> None:
        # The paren skipper walks to the end of the string when a group never
        # closes; PostgreSQL rejects the statement itself.
        with contextlib.suppress(QueryRejectedError):
            validate("SELECT * FROM (SELECT id FROM books")
