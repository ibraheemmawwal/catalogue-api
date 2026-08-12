"""Build a candidate manifest for the hosted demo catalogue.

The pipeline's own discovery reads a multi-gigabyte Open Library dump. That is
right for a real run and wrong for seeding a demo, so this asks Open Library's
search API for a bounded set of works instead and writes the same manifest
shape the pipeline already consumes.

Politeness is not incidental here: Open Library's policy discourages bulk
harvesting through the API, so this identifies itself, waits a second between
requests, and stops at a hard cap. The whole run is a few hundred requests
once, not a recurring job.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# Chosen for variety rather than volume: a demo needs series to order, authors
# to filter by, and enough spread of publication years for the statistics to
# say something.
SUBJECTS = [
    "science_fiction",
    "fantasy",
    "detective_and_mystery_stories",
    "historical_fiction",
    "classic_literature",
]
PER_SUBJECT = 100
FIELDS = (
    "key,title,subtitle,author_name,author_key,first_publish_year,isbn,"
    "language,number_of_pages_median,publisher,subject,cover_i"
)
CONTACT = os.environ.get("PIPELINE_OPENLIBRARY_CONTACT_EMAIL", "")


async def fetch_subject(client: httpx.AsyncClient, subject: str) -> list[dict[str, Any]]:
    response = await client.get(
        "https://openlibrary.org/search.json",
        params={"q": f"subject:{subject}", "fields": FIELDS, "limit": PER_SUBJECT},
    )
    response.raise_for_status()
    return list(response.json().get("docs", []))


def to_candidate(doc: dict[str, Any]) -> dict[str, Any] | None:
    """One search document as a manifest line, or None if unusable."""
    key, title = doc.get("key"), doc.get("title")
    if not key or not title:
        return None
    return {
        "candidate_key": key,
        "title": title,
        "authors": doc.get("author_name", [])[:5],
        "isbns": doc.get("isbn", [])[:8],
        "openlibrary_work_key": key,
        "openlibrary_edition_key": None,
        "languages": doc.get("language", [])[:3],
        # Retained so the resolver can promote it to a provenance-bearing
        # observation without spending a second request on the same book.
        "discovery_payload": doc,
    }


async def main() -> int:
    if not CONTACT:
        print(
            "PIPELINE_OPENLIBRARY_CONTACT_EMAIL is required — the source asks to be told who is calling."
        )
        return 1

    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "staging/candidates.jsonl")
    destination.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    written = 0

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": f"book-data-pipeline/2.0 (+{CONTACT})"},
    ) as client:
        with destination.open("w") as handle:  # noqa: ASYNC230
            for index, subject in enumerate(SUBJECTS):
                if index:
                    await asyncio.sleep(1.0)  # one request per second
                try:
                    docs = await fetch_subject(client, subject)
                except httpx.HTTPError as error:
                    print(f"  {subject}: skipped ({type(error).__name__})")
                    continue

                kept = 0
                for doc in docs:
                    candidate = to_candidate(doc)
                    # Subjects overlap; a book found twice is one candidate.
                    if candidate is None or candidate["candidate_key"] in seen:
                        continue
                    seen.add(candidate["candidate_key"])
                    handle.write(json.dumps(candidate) + "\n")
                    kept += 1
                written += kept
                print(f"  {subject}: {kept} new")

    print(f"wrote {written} candidates to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
