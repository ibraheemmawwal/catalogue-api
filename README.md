# catalogue-api

A read-only API over the book catalogue built by
[`book-data-pipeline`](https://github.com/ibraheemmawwal/book-data-pipeline).

It serves the same data two ways: **HTTP** for developers and **MCP** for AI
agents. Both go through one repository layer, so they cannot disagree.

## Why an MCP server

The catalogue records where every book came from — which sources supplied it,
which fields each contributed, and where they disagreed. That makes a question
possible that a general-purpose book API cannot answer:

> *"Who says this book was published in 1965, and does anything contradict it?"*

The MCP surface exists to expose that to an agent, not to restate the HTTP
routes in another protocol.

```python
mcp_servers = [{"type": "url", "name": "book-catalogue", "url": "https://<service>/mcp"}]
tools = [{"type": "mcp_toolset", "mcp_server_name": "book-catalogue"}]
```

No credential — attach it and ask.

| Tool | Answers |
|---|---|
| `search_books` | "Find me books like…" |
| `get_book` | "Tell me everything about this one" |
| `get_series` | "What order do I read these in?" |
| `get_book_provenance` | "Where did this come from, and do sources agree?" |
| `catalogue_stats` | "How complete is this data?" |

## HTTP

| Route | Purpose |
|---|---|
| `GET /live` `/ready` `/health` | Probes; liveness touches no database |
| `GET /v1/books` | Filterable, keyset-paginated collection |
| `GET /v1/books/search` | Full-text search across titles, authors and series |
| `GET /v1/books/{isbn13}` | One book by canonical identity |
| `GET /v1/series/{id}` | A series and its ordered members |
| `GET /v1/stats` | Coverage and provenance statistics |

Errors are RFC 9457 `application/problem+json`.

## Running it

```bash
uv sync --all-groups
uv run catalogue-api          # http://localhost:8000/docs
```

Requires `API_DATABASE_URL` pointing at a catalogue database. The API owns no
migrations: the pipeline owns the schema, and this service verifies a pinned
contract at startup rather than assuming it.

## Development

```bash
uv run ruff check . && uv run mypy src/     # gates
uv run pytest -m "not integration"          # unit
uv run pytest -m integration                # needs Docker
```
