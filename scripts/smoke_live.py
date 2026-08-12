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

TIMEOUT = 45.0  # Cloud Run cold start plus a Neon wake-up.


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")
        if not passed:
            self.failures.append(name)


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

        # The problem shape a client depends on.
        missing = await client.get("/v1/books/9780000000000")
        checks.check(
            "an unknown ISBN returns a problem document",
            missing.status_code == 404
            and missing.headers.get("content-type") == "application/problem+json",
        )

        # MCP. A 307 or 200 both mean the transport is mounted and answering;
        # a 404 means the mount path is wrong, which is the failure that
        # actually happened once.
        mcp = await client.get("/mcp", follow_redirects=False)
        checks.check(
            "/mcp is mounted",
            mcp.status_code in {200, 307, 400, 405, 406},
            f"status={mcp.status_code}",
        )

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
