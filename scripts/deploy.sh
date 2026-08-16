#!/usr/bin/env bash
#
# Build, verify on a URL that serves no traffic, then promote.
#
# The shape is the same everywhere and none of it is Google-specific: build an
# image, start it somewhere reachable but unpromoted, prove it works, and only
# then move traffic. What differs between clouds is the spelling, which lives
# in deploy/providers/<name>.sh behind five functions.
#
# Idempotent, in the sense that matters: the running service is a function of
# deploy/service.conf and the version argument, not of the sequence of commands
# anyone has typed at it. Deploying the same version twice is a no-op beyond a
# new revision. Deploying after someone has adjusted a setting by hand puts it
# back, because every setting is passed on every deploy.
#
# The gate is the point. Running the smoke script by hand after a deploy tells
# you what you already shipped; running it against a revision serving no
# traffic tells you whether to ship at all. It has already earned this: the
# first staged deploy failed on MCP with 421 and production never moved.
#
# Usage:  scripts/deploy.sh --check          what the target needs, and what is missing
#         scripts/deploy.sh v1.2.3        build, verify, promote
#
# The target lives in deploy/target.env (copy deploy/target.env.example). One
# variable picks the cloud; the rest are that cloud's names and identifiers.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_FILE="${ROOT}/deploy/target.env"

if [[ ! -f "${TARGET_FILE}" ]]; then
    cat >&2 <<TXT
No deployment target configured.

    cp deploy/target.env.example deploy/target.env
    \$EDITOR deploy/target.env
    scripts/deploy.sh --check

It is gitignored and holds no secrets — names and identifiers only.
TXT
    exit 2
fi
# set -a exports everything the file defines, so providers see it without each
# variable being listed twice.
set -a
# The target file is chosen at runtime, so shellcheck cannot follow it.
# shellcheck disable=SC1090
source "${TARGET_FILE}"
set +a
# shellcheck source=../deploy/service.conf
source "${ROOT}/deploy/service.conf"

PROVIDER="${DEPLOY_PROVIDER:?DEPLOY_PROVIDER is not set in deploy/target.env}"
PROVIDER_FILE="${ROOT}/deploy/providers/${PROVIDER}.sh"
if [[ ! -f "${PROVIDER_FILE}" ]]; then
    echo "unknown DEPLOY_PROVIDER '${PROVIDER}'." >&2
    printf 'available: ' >&2
    basename -a "${ROOT}"/deploy/providers/*.sh 2>/dev/null | sed 's/\.sh$//' | tr '\n' ' ' >&2
    printf '\nadding one is documented in deploy/README.md.\n' >&2
    exit 2
fi
# shellcheck source=/dev/null
source "${PROVIDER_FILE}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
smoke() { uv run python "${ROOT}/scripts/smoke_live.py" "$1"; }

# Everything the target needs, then whether this machine actually has it.
# Deliberately before the build: a missing secret or registry discovered after
# an image is pushed is the same error found at the most expensive moment.
preflight() {
    step "Target: ${PROVIDER}"
    provider_requirements
    printf '\n'
    if provider_preflight; then
        # Anything this target cannot honour, said before the deploy rather
        # than discovered from its behaviour afterwards.
        if declare -f provider_notes >/dev/null; then provider_notes; fi
        echo
        echo "preflight passed — ${PROVIDER} is ready."
        return 0
    fi
    echo >&2
    echo "preflight failed. Fix the above, then run scripts/deploy.sh --check again." >&2
    return 1
}

if [[ "${1:-}" == "--check" ]]; then
    preflight
    exit $?
fi

VERSION="${1:?usage: scripts/deploy.sh <version> | --check}"
preflight >/dev/null || { preflight; exit 1; }

step "Building ${VERSION} for ${PROVIDER}"
IMAGE="$(provider_build "${VERSION}")"

step "Deploying a candidate that takes no traffic"
# Checked explicitly rather than left to set -e. A function whose result is
# captured in a command substitution can swallow a non-zero exit from a command
# in its middle, and this pipeline promoting after a failed deploy is precisely
# the failure worth spending three lines on.
if ! CANDIDATE_URL="$(provider_deploy_candidate "${IMAGE}")"; then
    echo "candidate deploy failed — production is untouched." >&2
    exit 1
fi
[[ -n "${CANDIDATE_URL}" ]] || { echo "provider returned no candidate URL" >&2; exit 1; }

step "Smoke testing the candidate: ${CANDIDATE_URL}"
if ! smoke "${CANDIDATE_URL}"; then
    echo >&2
    echo "SMOKE FAILED — production is untouched, still serving its previous revision." >&2
    echo "The candidate is up at ${CANDIDATE_URL} for inspection." >&2
    exit 1
fi

step "Promoting ${VERSION}"
provider_promote

step "Verifying production"
PROD_URL="$(provider_url)"
# Again, against the real URL. The candidate answers on a different hostname,
# and a service can be correct on one and refused on another — which is how
# the second of Cloud Run's two URLs was refusing MCP unnoticed.
smoke "${PROD_URL}"

printf '\n\033[1mDeployed %s to %s.\033[0m\n  %s\n  Rollback: %s\n' \
    "${VERSION}" "${PROD_URL}" "${PROVIDER}" "$(provider_rollback_hint)"
