"""The schema pin must say the same thing everywhere it is written.

It lives in two places: the integration conftest, which is what a developer
runs against, and the CI workflow environment, which overrides it. When those
disagree the contract test still passes in both places — each validates a
different schema, and the cross-repository guarantee quietly stops being
checked at all.

That is exactly what happened: conftest was bumped to v2.1 and the workflow was
left on v2.0.1.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _workflow_pin() -> dict[str, str]:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return {
        key: match.group(1)
        for key in ("PIPELINE_SCHEMA_REF", "PINNED_ALEMBIC_REVISION")
        if (match := re.search(rf"^\s*{key}:\s*(\S+)\s*$", text, re.M))
    }


def _conftest_pin() -> dict[str, str]:
    text = (REPO / "tests" / "integration" / "conftest.py").read_text(encoding="utf-8")
    return {
        key: match.group(1)
        for key in ("PIPELINE_SCHEMA_REF", "PINNED_ALEMBIC_REVISION")
        if (match := re.search(rf'{key} = os\.environ\.get\("{key}", "([^"]+)"\)', text))
    }


class TestThePinAgreesWithItself:
    def test_both_places_name_the_same_ref(self) -> None:
        workflow, conftest = _workflow_pin(), _conftest_pin()

        assert workflow.get("PIPELINE_SCHEMA_REF") == conftest.get("PIPELINE_SCHEMA_REF"), (
            f"CI validates {workflow.get('PIPELINE_SCHEMA_REF')} but a local run "
            f"validates {conftest.get('PIPELINE_SCHEMA_REF')}"
        )

    def test_both_places_name_the_same_revision(self) -> None:
        workflow, conftest = _workflow_pin(), _conftest_pin()

        assert workflow.get("PINNED_ALEMBIC_REVISION") == conftest.get("PINNED_ALEMBIC_REVISION")

    def test_the_pin_is_actually_found_in_both(self) -> None:
        # A rename that makes both lookups return nothing would let the two
        # tests above pass by matching None against None.
        assert set(_workflow_pin()) == {"PIPELINE_SCHEMA_REF", "PINNED_ALEMBIC_REVISION"}
        assert set(_conftest_pin()) == {"PIPELINE_SCHEMA_REF", "PINNED_ALEMBIC_REVISION"}
