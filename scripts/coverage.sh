#!/usr/bin/env bash
# Combined coverage across every suite.
#
# The unit suite alone cannot reach the gate: the repository layer is SQL, and
# SQL is only meaningfully covered by running it. Measuring each suite
# separately and combining is the only number that reflects what is tested.
set -euo pipefail

rm -f .coverage .coverage.*

COVERAGE_FILE=.coverage.unit uv run pytest -q tests/unit/ \
  --cov=src --cov-report= --cov-fail-under=0

COVERAGE_FILE=.coverage.integration uv run pytest -q tests/integration/ \
  -p no:randomly --cov=src --cov-report= --cov-fail-under=0

uv run coverage combine
uv run coverage report --show-missing --fail-under=98
