"""Constructing and mounting the MCP server.

Streamable HTTP, mounted at ``/mcp`` in the same application as the REST
routes. That is the remote transport an MCP client expects; stdio is for a
server running as a local subprocess and would be meaningless for a deployed
service.

Tool schemas are derived from the type hints on each function, so a parameter's
Python type is its contract and there is no second schema to keep in step.
"""

from __future__ import annotations

from typing import Any

import structlog
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncEngine

from api.config import Settings
from api.mcp import descriptions
from api.mcp.tools import CatalogueTools

logger = structlog.get_logger(__name__)


def build_mcp_server(engine: AsyncEngine, settings: Settings) -> MCPServer:
    """An MCP server over the same engine the HTTP routes use."""
    tools = CatalogueTools(engine, settings)

    server = MCPServer(
        name="book-catalogue",
        title="Book Catalogue",
        instructions=descriptions.SERVER_INSTRUCTIONS,
    )

    @server.tool(name="search_books", description=descriptions.SEARCH_BOOKS)
    async def search_books(
        query: str | None = None,
        author: str | None = None,
        subject: str | None = None,
        series: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        language: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await tools.search_books(
            query=query,
            author=author,
            subject=subject,
            series=series,
            year_from=year_from,
            year_to=year_to,
            language=language,
            limit=limit,
        )

    @server.tool(name="get_book", description=descriptions.GET_BOOK)
    async def get_book(isbn13: str | None = None, id: int | None = None) -> dict[str, Any]:  # noqa: A002
        return await tools.get_book(isbn13=isbn13, id=id)

    @server.tool(name="get_series", description=descriptions.GET_SERIES)
    async def get_series(name: str | None = None, id: int | None = None) -> dict[str, Any]:  # noqa: A002
        return await tools.get_series(name=name, id=id)

    @server.tool(name="get_book_provenance", description=descriptions.GET_BOOK_PROVENANCE)
    async def get_book_provenance(
        isbn13: str | None = None,
        id: int | None = None,  # noqa: A002
    ) -> dict[str, Any]:
        return await tools.get_book_provenance(isbn13=isbn13, id=id)

    @server.tool(name="list_contested_books", description=descriptions.LIST_CONTESTED_BOOKS)
    async def list_contested_books(limit: int = 10) -> dict[str, Any]:
        return await tools.list_contested_books(limit=limit)

    @server.tool(name="catalogue_stats", description=descriptions.CATALOGUE_STATS)
    async def catalogue_stats() -> dict[str, Any]:
        return await tools.catalogue_stats()

    logger.info("mcp.server_built", tools=6)
    return server
