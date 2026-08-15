"""A per-instance ceiling on an unauthenticated surface.

What this is not: an edge rate limit. It counts requests inside one process,
and Cloud Run runs many, so the effective ceiling is this number times the
instance count and it resets whenever an instance is recycled. A determined
caller spreading requests across instances gets more than the configured
budget, and nothing here can see that happening.

What it is: the thing that stops one caller from exhausting one instance's
connection pool. ``/v1`` and ``/mcp`` are open by design, every request costs a
database connection out of a pool of five, and without a ceiling a single
enthusiastic client — or an agent in a retry loop, which is the likelier
version — takes the service down for everyone else. That failure is cheap to
cause by accident, and this makes it cost the caller 429s instead.

A gateway in front of this is still the right answer and this does not replace
it. It is what the service can enforce about itself, and it holds whether or
not the gateway is configured correctly, which is worth something on its own.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Health checks are never limited. Cloud Run's probes come from a small number
# of addresses at a steady rate, and a limiter that answered them 429 would
# take the instance out of rotation for being popular.
EXEMPT_PATHS = frozenset({"/live", "/ready"})

# The limiter must not become the memory problem it exists to prevent. Buckets
# are keyed by client address, an attacker picks the key, so the table is
# capped and the least recently used entry is evicted. Eviction is safe in the
# direction that matters: it forgets an idle caller's consumption, never
# invents budget for an active one, because an active caller stays at the
# recent end of the table.
MAX_TRACKED_CLIENTS = 10_000


@dataclass
class _Bucket:
    """A token bucket, refilled continuously rather than in windows.

    A fixed window lets a caller spend the whole budget in the last instant of
    one window and the whole of the next immediately after, which is twice the
    intended rate at exactly the moment load is highest.
    """

    tokens: float
    updated: float

    def take(self, *, rate: float, burst: float, now: float) -> bool:
        self.tokens = min(burst, self.tokens + (now - self.updated) * rate)
        self.updated = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def retry_after(self, *, rate: float) -> int:
        """Whole seconds until one token exists, never zero.

        A Retry-After of 0 invites an immediate retry, which is how a limiter
        turns a busy client into a hot loop.
        """
        return max(1, int((1.0 - self.tokens) / rate) + 1)


class RateLimiter:
    """The counting, separated from the ASGI plumbing so it can be tested."""

    def __init__(self, *, per_minute: int, burst: int) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(self, key: str, *, now: float | None = None) -> int | None:
        """``None`` to allow, or the seconds to wait before retrying."""
        moment = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._burst, updated=moment)
            self._buckets[key] = bucket
            if len(self._buckets) > MAX_TRACKED_CLIENTS:
                self._buckets.popitem(last=False)
        self._buckets.move_to_end(key)

        if bucket.take(rate=self._rate, burst=self._burst, now=moment):
            return None
        return bucket.retry_after(rate=self._rate)


def client_key(scope: Scope, *, trusted_proxies: int) -> str:
    """Who to charge for this request.

    ``X-Forwarded-For`` is caller-controlled at the left and proxy-appended at
    the right, so the trustworthy entries are counted from the end. Cloud Run
    appends one hop, so the default of 1 reads the address it observed.

    Set it too high and everyone shares a bucket keyed on a proxy address,
    which turns the limit into a global one. Set it too low and the key is
    whatever the caller typed, which makes the limit free to evade — so the
    fallback when the header is too short is the peer address, never an
    attacker-supplied value.
    """
    peer = scope.get("client")
    fallback = str(peer[0]) if peer else "unknown"
    if trusted_proxies <= 0:
        return fallback

    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name != b"x-forwarded-for":
            continue
        hops: list[str] = [
            part.strip() for part in value.decode("latin-1").split(",") if part.strip()
        ]
        if len(hops) >= trusted_proxies:
            return hops[-trusted_proxies]
        return fallback
    return fallback


@dataclass(frozen=True)
class RateLimitPolicy:
    """The four numbers and the hop count, kept together.

    One object rather than five arguments because they are only ever meaningful
    as a set: a burst without its rate, or a hop count applied to the wrong
    budget, is a misconfiguration that reads as a working limiter.
    """

    per_minute: int
    burst: int
    mcp_per_minute: int
    mcp_burst: int
    trusted_proxies: int = 1


class RateLimitMiddleware:
    """Pure ASGI, so it also covers the mounted MCP sub-application.

    ``BaseHTTPMiddleware`` buffers the response, which would break the MCP
    transport's streaming — and MCP is the surface most worth limiting, since
    one call can run a caller-supplied query.
    """

    def __init__(self, app: ASGIApp, policy: RateLimitPolicy) -> None:
        self._app = app
        self._general = RateLimiter(per_minute=policy.per_minute, burst=policy.burst)
        # Its own budget, and a smaller one. An MCP call can run arbitrary
        # SQL under a statement timeout; a REST call runs a query we wrote.
        # Sharing one budget would price them as though they cost the same.
        self._mcp = RateLimiter(per_minute=policy.mcp_per_minute, burst=policy.mcp_burst)
        self._trusted_proxies = policy.trusted_proxies

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        limiter = self._mcp if path.startswith("/mcp") else self._general
        retry_after = limiter.check(client_key(scope, trusted_proxies=self._trusted_proxies))
        if retry_after is None:
            await self._app(scope, receive, send)
            return

        await _too_many_requests(scope, send, retry_after=retry_after)


async def _too_many_requests(scope: Scope, send: Send, *, retry_after: int) -> None:
    """The same problem+json shape every other error here uses.

    Written directly rather than raised: this middleware sits outside the
    exception handlers, so a ProblemError from here would reach the server as
    an unhandled exception and become a 500.
    """
    body = json.dumps(
        {
            "type": "https://catalogue.example/problems/rate-limited",
            "title": "Too many requests",
            "status": 429,
            "detail": (
                f"Rate limit exceeded. Retry in {retry_after}s. "
                "This endpoint is unauthenticated and shared, so the limit is per client."
            ),
            "instance": scope.get("path", ""),
        }
    ).encode()

    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/problem+json"),
                (b"retry-after", str(retry_after).encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    message: Message = {"type": "http.response.body", "body": body}
    await send(message)
