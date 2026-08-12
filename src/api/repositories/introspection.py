"""Schema introspection and bounded read-only SQL.

Two capabilities that belong together because the first is what makes the
second usable: an agent cannot write a sensible query against a schema it
cannot see, and left to guess it writes `SELECT * FROM book` and gets an error
it has no way to recover from.

The SQL path is the only place in this service where a caller supplies a query
rather than choosing a tool, so it is also the only place where the boundary
has to be enforced rather than assumed. The rules are below, and every one of
them exists because the endpoint is unauthenticated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

logger = structlog.get_logger(__name__)

# The tables a query may touch. An allowlist rather than a denylist: a denylist
# has to anticipate every catalogue, extension and system view that might leak
# something, and it only has to be wrong once.
QUERYABLE_TABLES = frozenset(
    {
        "books",
        "authors",
        "book_authors",
        "subjects",
        "book_subjects",
        "series",
        "book_series",
        "book_sources",
        "ingestion_runs",
    }
)

MAX_ROWS = 200
# Role names come from configuration, but a name is interpolated into DDL that
# cannot be parameterised, so it is checked rather than trusted.
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
STATEMENT_TIMEOUT_MS = 5_000

# Anything that is not a read. Checked as whole words so a column named
# "updated_at" does not trip the "update" rule.
_FORBIDDEN = re.compile(
    r"\b("
    r"insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|analyze|reindex|cluster|comment|call|do|"
    r"set|reset|begin|commit|rollback|savepoint|listen|notify|"
    r"pg_read_file|pg_read_binary_file|pg_ls_dir|pg_sleep|lo_import|lo_export|dblink"
    r")\b",
    re.IGNORECASE,
)

# Comments are stripped before anything is checked. "FROM /*x*/ pg_authid"
# otherwise reads as a query with no table at all, and a comment is never
# load-bearing in a generated one-liner.
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

_SOURCE_CLAUSE = re.compile(r"\b(?:from|join)\b", re.IGNORECASE)
# Quoted, bare, or schema-qualified. Without the quoted form, `FROM "pg_authid"`
# matched nothing and sailed through as a query that touched no tables.
_QUALIFIED = re.compile(
    r'(?:"(?P<qschema>[^"]+)"|(?P<schema>[a-zA-Z_][a-zA-Z0-9_$]*))\s*\.\s*'
    r'(?:"(?P<qtable>[^"]+)"|(?P<table>[a-zA-Z_][a-zA-Z0-9_$]*))'
    r'|(?:"(?P<qonly>[^"]+)"|(?P<only>[a-zA-Z_][a-zA-Z0-9_$]*))'
)
# Words that end a relation list rather than naming another relation.
_NOT_A_RELATION = frozenset(
    {
        "where",
        "group",
        "order",
        "limit",
        "offset",
        "fetch",
        "having",
        "window",
        "union",
        "intersect",
        "except",
        "on",
        "using",
        "as",
        "select",
        "join",
        "inner",
        "left",
        "right",
        "full",
        "outer",
        "cross",
        "natural",
        "lateral",
        "with",
        "for",
    }
)


def _skip_space(text_: str, index: int) -> int:
    while index < len(text_) and text_[index].isspace():
        index += 1
    return index


def _skip_parens(text_: str, index: int) -> int:
    """Step over a balanced (...) group starting at ``index``."""
    depth = 0
    while index < len(text_):
        if text_[index] == "(":
            depth += 1
        elif text_[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return index


def referenced_tables(statement: str) -> set[str]:
    """Every relation named in a FROM or JOIN clause.

    Walks each clause instead of matching the first name after it. A single
    regex missed three things that all reach a forbidden table: a quoted
    identifier, a comment before the name, and the second entry in
    ``FROM books, pg_authid``.

    Subqueries and CTE references are not relations to check — the tables
    inside them are found by the same walk.
    """
    names: set[str] = set()
    for clause in _SOURCE_CLAUSE.finditer(statement):
        position = clause.end()
        while True:
            position = _skip_space(statement, position)
            if position >= len(statement):
                break

            if statement[position] == "(":
                position = _skip_parens(statement, position)
            else:
                found = _QUALIFIED.match(statement, position)
                if not found:
                    break
                position = found.end()
                schema = found.group("schema") or found.group("qschema")
                table = (
                    found.group("table")
                    or found.group("qtable")
                    or found.group("only")
                    or found.group("qonly")
                )
                after = _skip_space(statement, position)
                if after < len(statement) and statement[after] == "(":
                    # A set-returning function, not a table: pg_ls_dir() reads
                    # the filesystem and would otherwise look like a bare name.
                    names.add(f"{table}()")
                    position = _skip_parens(statement, after)
                elif schema and schema.lower() != "public":
                    names.add(f"{schema}.{table}".lower())
                else:
                    names.add(table.lower())

            # Step over any alias, then take another relation only if the list
            # continues with a comma.
            position = _skip_space(statement, position)
            while position < len(statement) and statement[position] not in ",)":
                word = re.match(r"[a-zA-Z_][a-zA-Z0-9_$]*", statement[position:])
                if word and word.group(0).lower() in _NOT_A_RELATION:
                    break
                position += len(word.group(0)) if word else 1
                position = _skip_space(statement, position)

            if position < len(statement) and statement[position] == ",":
                position += 1
                continue
            break
    return names


class QueryRejectedError(Exception):
    """A query that will not be run, and why.

    The message is written for a model to act on: it says which rule was
    broken and what would satisfy it, because "invalid query" ends the agent's
    turn while "only these tables are queryable: …" continues it.
    """


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


def validate(sql: str) -> str:
    """Check a query against every boundary, or refuse it.

    Returns the statement to run, with a LIMIT applied.

    Raises:
        QueryRejectedError: naming the rule and what would satisfy it.
    """
    statement = _COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    if not statement:
        msg = "The query was empty. Provide a single SELECT statement."
        raise QueryRejectedError(msg)

    # One statement only. Without this, "SELECT 1; DROP TABLE books" passes
    # every other check because the first half is a perfectly good read.
    if ";" in statement:
        msg = (
            "Only one statement may be run at a time. Remove the semicolon and "
            "send a single SELECT."
        )
        raise QueryRejectedError(msg)

    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        msg = (
            "Only SELECT queries are allowed (a WITH clause ending in SELECT is "
            "fine). This catalogue is read-only."
        )
        raise QueryRejectedError(msg)

    forbidden = _FORBIDDEN.search(statement)
    if forbidden:
        msg = (
            f"The keyword {forbidden.group(1)!r} is not allowed. Only reads are "
            "permitted, and only against the catalogue tables."
        )
        raise QueryRejectedError(msg)

    referenced = referenced_tables(statement)
    # Subquery aliases and CTE names appear here too; only names that are not
    # queryable *and* not defined in the query itself are a problem.
    defined = {
        name.lower()
        for name in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(", statement, re.IGNORECASE)
    }
    unknown = referenced - QUERYABLE_TABLES - defined
    if not referenced - defined:
        # No relation at all. Legitimate catalogue questions always read a
        # table; a query without one is asking the server about itself
        # ("SELECT current_setting('data_directory')").
        msg = (
            "A query must read from at least one catalogue table. Queryable "
            f"tables are: {', '.join(sorted(QUERYABLE_TABLES))}."
        )
        raise QueryRejectedError(msg)

    if unknown:
        msg = (
            f"Unknown or non-queryable table(s): {', '.join(sorted(unknown))}. "
            f"Queryable tables are: {', '.join(sorted(QUERYABLE_TABLES))}. "
            "Call describe_schema to see their columns."
        )
        raise QueryRejectedError(msg)

    # A LIMIT is added rather than required, so a caller does not have to know
    # the rule to write a working query. It asks for one row *more* than we
    # will return: without that spare row, a query matching exactly MAX_ROWS
    # and one matching ten thousand are indistinguishable, and the caller is
    # told a truncated page is the whole answer. An explicit LIMIT is left
    # alone and capped at fetch time instead.
    if not re.search(r"\blimit\s+\d+\s*$", lowered):
        statement = f"{statement}\nLIMIT {MAX_ROWS + 1}"

    return statement


async def run_query(
    connection: AsyncConnection, sql: str, *, readonly_role: str | None = None
) -> QueryResult:
    """Run a validated query inside a read-only, least-privilege transaction.

    Three boundaries, because the first one is the one most likely to be wrong.
    ``validate`` is a parser, and a parser written by hand had four evasions in
    its first draft. What backs it up is enforced by PostgreSQL: ``READ ONLY``
    means no statement can write whatever the parser believed, the statement
    timeout bounds a legal-but-enormous query, and ``SET LOCAL ROLE`` drops the
    transaction to a role granted ``SELECT`` on the queryable tables and
    nothing else.

    The role is not a complete substitute for the allowlist — PostgreSQL lets
    any role read much of ``pg_catalog`` — so both layers are load-bearing:
    the parser keeps system catalogues out, the role keeps this service's own
    non-public tables out.
    """
    statement = validate(sql)

    await connection.execute(text("SET TRANSACTION READ ONLY"))
    await connection.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))

    if readonly_role:
        if not _ROLE_NAME.match(readonly_role):
            msg = f"Configured SQL role {readonly_role!r} is not a valid identifier."
            raise QueryRejectedError(msg)
        try:
            await connection.execute(text(f'SET LOCAL ROLE "{readonly_role}"'))
        except DBAPIError as error:
            # Fail closed. Running as the owning role because the restricted
            # one is missing is exactly the silent downgrade this layer exists
            # to prevent.
            logger.error("sql.role_unavailable", role=readonly_role, error=str(error))
            msg = (
                "SQL querying is not available on this deployment: the read-only "
                "role is not configured. Use the other catalogue tools."
            )
            raise QueryRejectedError(msg) from error

    result = await connection.execute(text(statement))
    columns = list(result.keys())
    fetched = result.fetchmany(MAX_ROWS + 1)

    truncated = len(fetched) > MAX_ROWS
    rows = [dict(zip(columns, row, strict=False)) for row in fetched[:MAX_ROWS]]

    logger.info("sql.executed", rows=len(rows), truncated=truncated)
    return QueryResult(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)


async def describe(connection: AsyncConnection) -> dict[str, Any]:
    """Columns and row counts for the queryable tables.

    Row counts come from the planner's estimate rather than count(*): the exact
    figure costs a scan of every table to answer a question about shape, and an
    agent deciding whether a table is worth querying does not need it precise.
    """
    columns = await connection.execute(
        text(
            """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ANY(:tables)
            ORDER BY table_name, ordinal_position
            """
        ),
        {"tables": sorted(QUERYABLE_TABLES)},
    )

    estimates = await connection.execute(
        text(
            """
            SELECT relname, GREATEST(reltuples::bigint, 0) AS rows
            FROM pg_class
            WHERE relname = ANY(:tables) AND relkind = 'r'
            """
        ),
        {"tables": sorted(QUERYABLE_TABLES)},
    )
    row_estimates = {row.relname: row.rows for row in estimates}

    tables: dict[str, Any] = {}
    for row in columns:
        entry = tables.setdefault(
            row.table_name,
            {"approximate_rows": row_estimates.get(row.table_name, 0), "columns": []},
        )
        entry["columns"].append(
            {
                "name": row.column_name,
                "type": row.data_type,
                "nullable": row.is_nullable == "YES",
            }
        )

    return tables
