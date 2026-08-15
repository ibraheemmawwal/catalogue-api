"""The counting and the client key, without an app around them.

The middleware is a thin wrapper over these two, and they are where the ways
to be wrong live: a bucket that refills in windows lets a caller spend two
budgets back to back, and a key taken from the wrong end of a header lets them
choose their own bucket.
"""

from __future__ import annotations

import pytest

from api.rate_limit import EXEMPT_PATHS, MAX_TRACKED_CLIENTS, RateLimiter, client_key


def scope(*, peer: str | None = "10.0.0.1", forwarded: str | None = None) -> dict:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return {"type": "http", "headers": headers, "client": (peer, 1234) if peer else None}


class TestTheBudget:
    def test_a_burst_is_allowed_then_refused(self) -> None:
        limiter = RateLimiter(per_minute=60, burst=5)

        allowed = [limiter.check("a", now=0.0) for _ in range(5)]
        refused = limiter.check("a", now=0.0)

        assert allowed == [None] * 5
        assert refused is not None

    def test_it_refills_continuously_rather_than_in_windows(self) -> None:
        """The property a fixed window does not have.

        A window lets a caller spend everything at 59s and everything again at
        61s — twice the intended rate, at the worst moment. One token a second
        should buy exactly one more request a second later, not five.
        """
        limiter = RateLimiter(per_minute=60, burst=5)
        for _ in range(5):
            limiter.check("a", now=0.0)

        assert limiter.check("a", now=1.0) is None, "a second should buy one token"
        assert limiter.check("a", now=1.0) is not None, "it should buy only one"

    def test_the_bucket_does_not_fill_past_its_burst(self) -> None:
        # Otherwise an idle caller accrues an unbounded allowance and returns
        # with it, which is the burst limit meaning nothing.
        limiter = RateLimiter(per_minute=60, burst=3)

        allowed = [limiter.check("a", now=3600.0) for _ in range(4)]

        assert allowed[:3] == [None] * 3
        assert allowed[3] is not None

    def test_callers_do_not_share_a_budget(self) -> None:
        limiter = RateLimiter(per_minute=60, burst=1)
        limiter.check("a", now=0.0)

        assert limiter.check("b", now=0.0) is None

    def test_retry_after_is_never_zero(self) -> None:
        """Zero invites an immediate retry, which is a hot loop.

        The caller most likely to be limited is an agent already retrying.
        """
        limiter = RateLimiter(per_minute=60, burst=1)
        limiter.check("a", now=0.0)

        assert limiter.check("a", now=0.0) >= 1  # type: ignore[operator]


class TestItsOwnMemory:
    def test_tracking_is_bounded(self) -> None:
        """The limiter must not become the exhaustion it prevents.

        The key is the caller's address, so the caller chooses it, and an
        unbounded table is a memory attack with a smaller packet.
        """
        limiter = RateLimiter(per_minute=60, burst=1)

        for index in range(MAX_TRACKED_CLIENTS + 100):
            limiter.check(f"client-{index}", now=0.0)

        assert len(limiter._buckets) <= MAX_TRACKED_CLIENTS

    def test_eviction_forgets_the_idle_not_the_active(self) -> None:
        """Eviction can only ever hand back budget, so it must hit the idle.

        An active caller stays at the recent end of the table, so the entry
        dropped is one that has not been seen — which is the difference between
        forgetting an old visitor and resetting the limit for the caller
        currently hitting it.
        """
        limiter = RateLimiter(per_minute=60, burst=1)
        limiter.check("busy", now=0.0)

        for index in range(MAX_TRACKED_CLIENTS + 10):
            limiter.check(f"other-{index}", now=0.0)
            limiter.check("busy", now=0.0)

        assert limiter.check("busy", now=0.0) is not None, "the active caller was forgotten"


class TestWhoGetsCharged:
    def test_the_peer_address_is_used_when_nothing_is_in_front(self) -> None:
        assert client_key(scope(forwarded="1.2.3.4"), trusted_proxies=0) == "10.0.0.1"

    def test_one_hop_reads_what_the_proxy_observed(self) -> None:
        # Cloud Run appends the address it saw, so the rightmost entry is the
        # only one it wrote.
        key = client_key(scope(forwarded="203.0.113.9, 10.0.0.1"), trusted_proxies=1)

        assert key == "10.0.0.1"

    def test_a_forged_header_cannot_choose_the_bucket(self) -> None:
        """The evasion this counting exists to stop.

        A caller sending its own X-Forwarded-For prepends to the list. Reading
        from the left would let it pick a fresh bucket per request and ignore
        the limit entirely.
        """
        forged = "evil-1, evil-2, evil-3, 10.0.0.1"

        assert client_key(scope(forwarded=forged), trusted_proxies=1) == "10.0.0.1"

    def test_a_header_shorter_than_expected_falls_back_to_the_peer(self) -> None:
        # Never to an attacker-supplied value: a caller could otherwise send a
        # single-entry header and be charged to whatever it named.
        key = client_key(scope(forwarded="1.2.3.4"), trusted_proxies=2)

        assert key == "10.0.0.1"

    def test_a_missing_peer_is_still_a_key(self) -> None:
        # ASGI allows no client, and a crash here would be a 500 on every
        # request rather than a rate limit.
        assert client_key(scope(peer=None), trusted_proxies=0) == "unknown"


@pytest.mark.parametrize("path", ["/live", "/ready"])
def test_health_paths_are_named_as_exempt(path: str) -> None:
    """Limiting a probe takes the instance out of rotation for being busy."""
    assert path in EXEMPT_PATHS
