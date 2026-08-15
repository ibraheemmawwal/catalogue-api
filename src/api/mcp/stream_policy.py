"""Refusing the server-to-client stream this server never uses.

MCP's Streamable HTTP transport lets a client open ``GET /mcp`` and hold it
open so the server can push requests and notifications without being asked.
This server pushes nothing: it is built ``stateless_http=True``, every tool is
request-response, and there is no subscription, progress or sampling anywhere
in it. The stream is opened, kept alive, and never carries a byte.

That would be merely untidy if it were free. On Cloud Run it is the entire
bill. Billing counts the duration of a request, streaming included, so a
connection held open for 61 seconds bills 61 seconds of CPU and memory. The
client reopens it the moment it lapses, which measured as 57 requests an hour,
around the clock, at 90% occupancy:

    GET   545 requests   avg 61.00s each
    POST   55 requests   avg 12.20s each

A service with no minimum instances and nothing scheduled was billed as though
it were always on, because in every way that costs money it was. Roughly $54 a
month against a $20 budget, for a stream carrying nothing.

The spec is unambiguous about the remedy: a server that does not offer an SSE
stream at this endpoint MUST return 405. Clients are required to accept that
and carry on over POST, which is where every real message already goes. So
this is the transport being told the truth about itself rather than a
workaround.

Kept behind a setting because it stops being true the moment the server gains
anything that pushes — progress notifications on a long tool call, say. If
that happens, this must be turned off in the same change, and the comment on
``mcp_offer_server_stream`` says so.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# POST carries every client-to-server message; DELETE ends a session. Both stay.
ALLOWED_METHODS = "POST, DELETE"


def _is_mcp_path(path: str, prefix: str) -> bool:
    """The mount and nothing under it.

    ``/mcp`` and ``/mcp/`` are the same endpoint — the sub-app is mounted at
    the prefix and serves its own root — so both are refused. A path that
    merely starts with the same letters is not: ``/mcpx`` is somebody else's
    route, and a prefix test alone would swallow it.
    """
    return path in (prefix, f"{prefix}/")


class RefuseServerStreamMiddleware:
    """Answer ``GET`` on the MCP mount with 405 before the transport sees it.

    Pure ASGI, because the mounted sub-application is not a FastAPI route and
    a router-level dependency would never run for it.
    """

    def __init__(self, app: ASGIApp, *, path_prefix: str = "/mcp") -> None:
        self._app = app
        self._prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and _is_mcp_path(scope.get("path", ""), self._prefix)
        ):
            await _method_not_allowed(scope, send)
            return
        await self._app(scope, receive, send)


async def _method_not_allowed(scope: Scope, send: Send) -> None:
    """405 with ``Allow``, in the same problem+json shape as every other error.

    ``Allow`` is required on a 405 and is also the useful half of the answer:
    a client reading it learns the endpoint is alive and which verb to use,
    rather than concluding MCP is unavailable here.
    """
    body = json.dumps(
        {
            "type": "https://catalogue.example/problems/no-server-stream",
            "title": "Method not allowed",
            "status": 405,
            "detail": (
                "This MCP endpoint is stateless and sends no server-initiated "
                "messages, so it offers no SSE stream. Send requests as POST."
            ),
            "instance": scope.get("path", ""),
        }
    ).encode()

    await send(
        {
            "type": "http.response.start",
            "status": 405,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"allow", ALLOWED_METHODS.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    message: Message = {"type": "http.response.body", "body": body}
    await send(message)
