"""Which requests the refusal applies to, without an app around it.

The integration tests prove a real client survives it. These pin the boundary:
one verb, on one path, and nothing else — because a middleware that sits in
front of every request is the wrong place to be approximately right.
"""

from __future__ import annotations

import pytest

from api.mcp.stream_policy import ALLOWED_METHODS, _is_mcp_path


class TestWhichPathsAreTheMount:
    @pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
    def test_both_spellings_of_the_endpoint(self, path: str) -> None:
        """``/mcp`` redirects to ``/mcp/``, so both have to be covered.

        Covering only the bare form would send every real client through the
        redirect and onto the stream, which is the entire thing being stopped.
        """
        assert _is_mcp_path(path, "/mcp") is True

    @pytest.mark.parametrize(
        "path",
        ["/mcpx", "/mcp-tools", "/v1/books", "/live", "/", "/v1/mcp"],
    )
    def test_nothing_else(self, path: str) -> None:
        # A prefix test alone would swallow /mcpx, which is somebody else's
        # route, and the failure would be a 405 on a working endpoint.
        assert _is_mcp_path(path, "/mcp") is False


def test_the_remaining_methods_are_named() -> None:
    """POST carries every client message; DELETE ends a session.

    Advertising a verb the transport does not handle would send a client to a
    405 from somewhere deeper, which is worse than not mentioning it.
    """
    assert ALLOWED_METHODS == "POST, DELETE"
