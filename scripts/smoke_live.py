"""Smoke-test a deployed instance.

Run against the real URL after a deploy. Everything here is a claim the README
makes to a reviewer, so a failure means the link is worse than useless — it is
misleading.

Deliberately not part of the test suite: it needs a deployment, and a suite
that cannot run offline is a suite people stop running.
"""

from __future__ import annotations

import asyncio
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

TIMEOUT = 45.0  # Cloud Run cold start plus a Neon wake-up.


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")
        if not passed:
            self.failures.append(name)


async def check_mcp(base: str, checks: Checks) -> None:
    """Complete a handshake and call a tool, the way an agent would."""
    expected = {
        "search_books",
        "get_book",
        "get_series",
        "get_book_provenance",
        "list_contested_books",
        "catalogue_stats",
        "describe_schema",
        "run_sql",
    }
    try:
        async with (
            streamable_http_client(f"{base}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            checks.check("MCP handshake completes", True)
            checks.check("every tool is offered", names == expected, ", ".join(sorted(names)))

            schema = (await session.call_tool("describe_schema", {})).structured_content or {}
            checks.check(
                "the schema is introspectable",
                "books" in (schema.get("tables") or {}),
                ", ".join(sorted(schema.get("tables") or {})),
            )

            sql = (
                await session.call_tool("run_sql", {"query": "SELECT count(*) AS n FROM books"})
            ).structured_content or {}
            checks.check(
                "a read-only query runs",
                bool(sql.get("rows")) and sql["rows"][0].get("n", 0) > 0,
                str(sql.get("error") or sql.get("rows")),
            )

            # The check that matters most in production: the deployed service,
            # not the local parser, refuses the write.
            refused = (
                await session.call_tool("run_sql", {"query": "DROP TABLE books"})
            ).structured_content or {}
            checks.check("a write is refused", "error" in refused, str(refused)[:120])

            body = (await session.call_tool("catalogue_stats", {})).structured_content or {}
            checks.check(
                "an MCP tool returns real data",
                body.get("books", 0) > 0,
                f"{body.get('books', 0)} books",
            )
    except Exception as error:
        checks.check("MCP handshake completes", False, f"{type(error).__name__}: {error}"[:90])


async def main(base: str) -> int:
    checks = Checks()

    async with httpx.AsyncClient(base_url=base, timeout=TIMEOUT, follow_redirects=True) as client:
        # Probes. /live must answer without a database, so it is checked first
        # and separately — if it fails, nothing else is worth reading.
        live = await client.get("/live")
        checks.check("/live answers 200", live.status_code == 200)

        ready = await client.get("/ready")
        checks.check(
            "/ready reports the schema compatible",
            ready.status_code == 200 and ready.json().get("schema") == "compatible",
            ready.text[:80],
        )

        docs = await client.get("/docs")
        checks.check("/docs renders", docs.status_code == 200)

        # Content. An empty catalogue passes every structural check and is
        # useless to a visitor, so emptiness is a failure here.
        books = await client.get("/v1/books", params={"limit": 5})
        items = books.json().get("items", []) if books.status_code == 200 else []
        checks.check("/v1/books returns books", len(items) > 0, f"{len(items)} returned")

        if items:
            first = items[0]
            checks.check("a book carries a title", bool(first.get("title")))

            page = await client.get("/v1/books", params={"limit": 2})
            cursor = page.json().get("next_cursor")
            checks.check("pagination offers a cursor", bool(cursor))
            if cursor:
                second = await client.get("/v1/books", params={"limit": 2, "cursor": cursor})
                second_ids = {b["id"] for b in second.json()["items"]}
                first_ids = {b["id"] for b in page.json()["items"]}
                checks.check(
                    "the second page does not repeat the first",
                    second.status_code == 200 and not (second_ids & first_ids),
                )

        search = await client.get("/v1/books/search", params={"q": "the"})
        checks.check(
            "search returns ranked results",
            search.status_code == 200 and len(search.json().get("items", [])) > 0,
            f"mode={search.json().get('mode')}" if search.status_code == 200 else search.text[:60],
        )

        stats = await client.get("/v1/stats")
        body = stats.json() if stats.status_code == 200 else {}
        checks.check(
            "/v1/stats reports a non-empty catalogue",
            body.get("books", 0) > 0,
            f"{body.get('books', 0)} books",
        )
        checks.check("statistics name their sources", bool(body.get("sources")))

        # The problem shape a client depends on, and the distinction the API
        # makes between the two ways an ISBN can fail. A malformed one is a
        # 400 — "you mistyped it" — and only a well-formed but absent one is a
        # 404. Conflating them is what the first draft of this check did.
        malformed = await client.get("/v1/books/9780000000000")
        checks.check(
            "a malformed ISBN returns a 400 problem document",
            malformed.status_code == 400
            and malformed.headers.get("content-type") == "application/problem+json",
            f"status={malformed.status_code}",
        )

        # Valid check digit, deliberately not a real published ISBN. Getting
        # this wrong the first time made the check assert 400 twice and prove
        # nothing about the 404 path.
        absent = await client.get("/v1/books/9785550000007")
        checks.check(
            "a well-formed but absent ISBN returns a 404 problem document",
            absent.status_code == 404
            and absent.headers.get("content-type") == "application/problem+json",
            f"status={absent.status_code}",
        )

    # MCP, over a real client session. Checking that /mcp merely answers is
    # not enough: it returned 307 while the handshake was failing with a 421,
    # so a status-code check passed against a completely broken surface.
    await check_mcp(base, checks)

    print()
    if checks.failures:
        print(f"FAILED: {', '.join(checks.failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: smoke_live.py https://host")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1].rstrip("/"))))
