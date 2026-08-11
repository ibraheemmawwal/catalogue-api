"""Shared fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Settings rejects unknown API_* variables, so a stray one in the developer's
# shell would fail every test with a confusing message. Clear the namespace and
# supply exactly what the tests intend.
_MANAGED_PREFIX = "API_"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in [n for n in os.environ if n.upper().startswith(_MANAGED_PREFIX)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("API_DATABASE_URL", "postgresql://user:pw@localhost:5432/catalogue")
    yield
