# Cloud Run.
#
# Implements the four functions scripts/deploy.sh calls. Everything specific to
# Google lives here; everything true of the service regardless of host lives in
# deploy/service.conf.

: "${PROJECT:=book-data-pipeline-2026}"
: "${REGION:=europe-west2}"
: "${REGISTRY:=europe-west2-docker.pkg.dev/${PROJECT}/catalogue}"
: "${SERVICE_ACCOUNT:=catalogue-api@${PROJECT}.iam.gserviceaccount.com}"

# One fixed tag, reused by every deploy, rather than one per version.
#
# Necessity rather than tidiness: the tag becomes a hostname, and the MCP
# transport answers only for hosts named in API_MCP_ALLOWED_HOSTS. A tag that
# changed per version would produce a hostname that could never be in that
# list, so the staging smoke test's MCP checks would fail on every deploy
# forever. One tag has one hostname, and it is in the list.
CANDIDATE_TAG=staging

provider_build() {
    local version="$1" image="${REGISTRY}/catalogue-api:$1"
    gcloud builds submit --config cloudbuild.yaml \
        --substitutions="_IMAGE=${image}" --project "${PROJECT}" --quiet >&2
    printf '%s' "${image}"
}

# Every setting is passed every time, so the result does not depend on what the
# service looked like beforehand. A flag dropped from this list becomes drift
# that survives the next deploy, which is exactly what this file exists to stop.
#
# An array, not a string. Word-splitting an unquoted expansion works right up
# until a value contains a space, and then it fails as a wrong-but-plausible
# argument rather than an error.
_deploy_flags() {
    DEPLOY_FLAGS=(
        --region="${REGION}" --project="${PROJECT}"
        --port="${CONTAINER_PORT}"
        --cpu="${CPU}" --memory="${MEMORY}"
        --concurrency="${CONCURRENCY}"
        --max-instances="${MAX_INSTANCES}"
        --timeout="${REQUEST_TIMEOUT_SECONDS}s"
        --service-account="${SERVICE_ACCOUNT}"
        --set-secrets="API_DATABASE_URL=${DATABASE_URL_SECRET}:latest"
        # ^@@^ picks a delimiter other than the comma, because the value is a
        # JSON list full of them. It belongs at the front of the *value*: put
        # it before the flag name and gcloud reports an unrecognised argument.
        --set-env-vars="^@@^API_MCP_ALLOWED_HOSTS=${API_MCP_ALLOWED_HOSTS}"
    )
}

_candidate_revision() {
    gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --project "${PROJECT}" \
        --format="value(status.traffic.filter(\"tag:${CANDIDATE_TAG}\").extract(\"revisionName\").flatten())" 2>/dev/null
}

provider_deploy_candidate() {
    local image="$1" before after
    _deploy_flags
    before="$(_candidate_revision)"

    # --no-traffic: the revision exists and is reachable at its tag, while
    # production keeps serving whatever it was already serving.
    if ! gcloud run deploy "${SERVICE_NAME}" --image "${image}" \
        --tag "${CANDIDATE_TAG}" --no-traffic --quiet "${DEPLOY_FLAGS[@]}" >&2; then
        echo "deploy failed for ${image}" >&2
        return 1
    fi

    # The tag must have moved to a new revision.
    #
    # A failed deploy leaves the previous revision sitting at the tag, so the
    # tag still resolves and the smoke test still passes — against the thing
    # already in production. A green gate on an unverified build is worse than
    # a red one. This has happened here: a malformed flag failed the deploy,
    # the pipeline carried on, and the candidate URL answered from the revision
    # before it.
    #
    # Revision names rather than image references, because Cloud Run stores the
    # digest it resolved rather than the tag it was given, so comparing against
    # the tag we asked for never matches and the guard cries wolf every time.
    after="$(_candidate_revision)"
    if [[ -z "${after}" || "${after}" == "${before}" ]]; then
        echo "the candidate tag did not move (still '${before:-unset}') — refusing to verify a stale revision" >&2
        return 1
    fi

    provider_candidate_url
}

provider_candidate_url() {
    gcloud run services describe "${SERVICE_NAME}" \
        --region "${REGION}" --project "${PROJECT}" \
        --format="value(status.traffic.filter(\"tag:${CANDIDATE_TAG}\").extract(\"url\").flatten())"
}

provider_promote() {
    gcloud run services update-traffic "${SERVICE_NAME}" \
        --region "${REGION}" --project "${PROJECT}" --to-latest --quiet >&2
}

provider_url() {
    gcloud run services describe "${SERVICE_NAME}" \
        --region "${REGION}" --project "${PROJECT}" --format="value(status.url)"
}

provider_rollback_hint() {
    printf 'gcloud run services update-traffic %s --region %s --to-revisions <previous>=100' \
        "${SERVICE_NAME}" "${REGION}"
}
