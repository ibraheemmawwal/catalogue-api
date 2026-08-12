# catalogue-api

**Live:** [https://book-catalogue-27035467540.europe-west2.run.app/docs](https://book-catalogue-27035467540.europe-west2.run.app/docs)
**MCP endpoint:** `https://book-catalogue-27035467540.europe-west2.run.app/mcp`

A read-only API over the book catalogue built by
[`book-data-pipeline`](https://github.com/ibraheemmawwal/book-data-pipeline).

It serves the same data two ways — **HTTP** for developers, **MCP** for AI
agents — through one repository layer, so the two cannot disagree. A test
asserts exactly that.

## Why an MCP server

The catalogue records where every book came from: which sources supplied it,
when each was last read, and where they disagreed. That makes a question
possible which a single-source book API cannot ask:

> *Who says this book was published in 1965, and does anything contradict it?*

The MCP surface exists for that, not to restate the HTTP routes in another
protocol.

```python
mcp_servers = [
    {
        "type": "url",
        "name": "book-catalogue",
        "url": "https://book-catalogue-27035467540.europe-west2.run.app/mcp",
    }
]
tools = [{"type": "mcp_toolset", "mcp_server_name": "book-catalogue"}]
```

No credential — attach it and ask.

The service scales to zero and the database suspends when idle, so the first
request after a quiet spell takes a few seconds. Subsequent ones are fast. That
is the free tier working as intended, not a fault.

| Tool | Answers |
|---|---|
| `search_books` | "Find me books like…" |
| `get_book` | "Tell me everything about this one" |
| `get_series` | "What order do I read these in?" |
| `get_book_provenance` | "Where did this come from, and do sources agree?" |
| `list_contested_books` | "Which records should I not trust?" |
| `catalogue_stats` | "How complete is this data?" |
| `describe_schema` | "What can I query?" |
| `run_sql` | "How many books per decade?" — anything the tools above cannot express |

A real answer from the live service:

```
get_book_provenance("The Fall of Hyperion")
  sources: goodreads, googlebooks, openlibrary
  published_year:  googlebooks "1990-02-01"  vs  openlibrary "1990"   -> kept 1990
  title:           goodreads "…(Hyperion Cantos, #2)"                 -> kept "The Fall of Hyperion"
```

Disagreements are reported, not resolved. The sources are not equally reliable
— one is an unofficial scrape used only to adjudicate records where the
documented sources already conflict — so a conflict is information the caller
should see rather than something to hide behind a single confident value.

### Letting an agent write SQL

The six typed tools answer the questions worth naming. `run_sql` exists for the
ones that are not worth naming — aggregates, groupings, cross-table comparisons
— and `describe_schema` exists because an agent cannot write a sensible query
against a schema it cannot see. They ship together for that reason.

It is the only place in this service where a caller supplies a query rather
than choosing a tool, and the endpoint is unauthenticated, so the boundary is
three layers deep:

1. **The parser.** A single statement, `SELECT` only, and only against nine
   catalogue tables — an allowlist, because a denylist has to anticipate every
   system view and only has to be wrong once. Rejections name the rule and the
   remedy, so an agent corrects itself instead of stopping.
2. **The transaction.** `READ ONLY` with a five-second statement timeout,
   enforced by PostgreSQL rather than by parsing.
3. **The role.** `SET LOCAL ROLE catalogue_readonly`, granted `SELECT` on those
   nine tables and nothing else.

The third layer is there because the first is the one most likely to be wrong.
The parser's first draft accepted `FROM "pg_authid"`, `FROM /*x*/ pg_authid`,
`FROM books, pg_authid` and `SELECT version()` — a quoted identifier, a
comment, the second entry in a comma list, and a query reading no table at all.
Each is now a test, and the role is what would have contained them.

Provision the role once per database, including locally:

```bash
psql "$API_DATABASE_URL" -f scripts/sql/readonly_role.sql
```

Without it `run_sql` fails closed and says so. Running with the service's own
grants because the restricted role is missing is the silent downgrade the layer
exists to prevent.

Three things shape the tool design:

- **Responses are sized for a context window.** Null fields are dropped, subject
  lists truncate with a remainder count, and search returns a projection. An
  unnecessary field is not a few bytes once — it is a cost on every call.
- **No cursors.** An agent threading an opaque token across turns loses its
  place. `search_books` reports that more matches exist and suggests narrowing,
  which is something a model can act on.
- **Errors are instructions.** "No book with ISBN 9780000000000 — use
  search_books to find its identifier" continues the agent's turn; a bare 404
  ends it.

## HTTP

| Route | Purpose |
|---|---|
| `GET /live` `/ready` `/health` | Probes; liveness touches no database |
| `GET /v1/books` | Filterable, keyset-paginated collection |
| `GET /v1/books/search` | Full-text search with a fuzzy fallback |
| `GET /v1/books/{isbn13}` | One book by canonical identity |
| `GET /v1/series/{id}` | A series and its books in reading order |
| `GET /v1/stats` | Coverage and provenance statistics |

Errors are RFC 9457 `application/problem+json`.

### Pagination

Keyset on `(lower(title), id)`, not offset. The pipeline writes while the API
reads, and under `OFFSET n` a row inserted earlier in the sort order shifts
everything after it — a client paging through sees a book twice or misses one,
with no error either time.

Sorting by publication year would have been the obvious choice and is unusable:
about a third of the catalogue has no year, and NULLs cannot anchor a cursor.

### Search

`websearch_to_tsquery` against the pipeline's generated `search_vector`, falling
back to trigram similarity when full text finds nothing at all. Ranks are
`numeric`, rounded before ordering, and never pass through a float — a rank that
shifts in its last bits names a row that does not exist, and the page boundary
then skips or repeats.

## Schema ownership

This service owns no migrations. The pipeline creates the tables; this API only
selects from them, and pins the columns it reads in a contract checked at
startup. `/ready` fails with the missing column named, rather than a 500 on one
endpoint in production days later.

Integration tests apply the pipeline's own migrations at a pinned tag. A schema
reconstructed from the contract would have no `pg_trgm` — so the trigram
operator the author and series filters use would not parse — and no generated
`search_vector`. The two features most likely to break are the two a
reconstruction cannot reproduce.

## Running it

```bash
uv sync --all-groups
cp .env.example .env        # then fill in API_DATABASE_URL
uv run catalogue-api        # http://localhost:8000/docs
```

`.env` is read for local runs only and is gitignored. The deployed service does
not use it: its database URL comes from Secret Manager, because a credential set
as a plain Cloud Run environment variable is readable by anyone with console
access and appears in `gcloud run services describe`.

Tests ignore `.env` entirely — otherwise whether the suite passes would depend on
an untracked file on one machine.

```bash
docker build -f docker/api.Dockerfile -t catalogue-api .
docker run -p 8000:8000 -e API_DATABASE_URL=postgresql://... catalogue-api
```

## Development

```bash
uv run ruff check . && uv run mypy src/    # gates
uv run pytest tests/unit/                  # fast
uv run pytest tests/integration/           # needs Docker
./scripts/coverage.sh                      # combined gate, 98%
```

Integration tests clone the pipeline at `PIPELINE_SCHEMA_REF` and apply its
migrations to a throwaway container. Point `PIPELINE_LOCAL_PATH` at a working
copy to skip the clone.

The coverage gate is on the combined report. The repository layer is SQL, and
SQL is only meaningfully covered by running it — a unit-only gate would either
fail honestly or be lowered until it meant nothing.
