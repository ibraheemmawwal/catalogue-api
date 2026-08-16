# shellcheck shell=bash
# AWS App Runner.
#
# Implemented, and NOT VERIFIED. Every function is written, and none of it has
# ever run against an AWS account — there was none available to test with. Read
# it as a careful first draft rather than a working path: the shapes come from
# the App Runner API, but the first real run will find something, and it should
# be done somewhere it can fail harmlessly.
#
# What to expect on that first run, in rough order of likelihood: an IAM
# permission the roles do not carry, a health check that fails because /live is
# reached before the database is, and the wait loop timing out because App
# Runner's first create takes longer than a Cloud Run revision does.
#
# What is genuinely different, and why this is not a translation exercise:
#
#   - No per-revision URL. There is no equivalent of a Cloud Run revision tag,
#     so a candidate that serves no traffic has to be a second service. That is
#     what AWS_STAGING_SERVICE is, and it costs what a second service costs.
#   - No request timeout. App Runner's is fixed at 120s, so the 20s ceiling
#     that caps a held connection here cannot be carried across.
#   - Fixed cpu/memory pairs, so service.conf's values have to be mapped and
#     some combinations cannot be expressed at all.
#
# The first two are surfaced by provider_notes on every deploy; the third
# refuses rather than rounding.

REGION="${AWS_REGION:-}"
ACCOUNT="${AWS_ACCOUNT_ID:-}"
REGISTRY="${ACCOUNT:+${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${AWS_ECR_REPOSITORY:-}}"
DATABASE_URL_SECRET="${AWS_DATABASE_URL_SECRET:-}"

# Every aws call goes through this.
#
# With no credentials configured, the cli falls back to looking for an EC2
# instance metadata endpoint, and on a laptop that is not a quick failure — it
# is a long wait on a link-local address that will never answer. A preflight
# whose job is to fail fast must not be the thing that hangs, and this is not
# hypothetical: it hung a shell here.
#
# The metadata lookup is disabled outright and both socket timeouts are short.
# A retry count of zero because a check that has already failed twice should
# report, not persevere.
_aws() {
    AWS_EC2_METADATA_DISABLED=true \
    AWS_RETRY_MODE=standard \
    AWS_MAX_ATTEMPTS=1 \
    aws --cli-connect-timeout 5 --cli-read-timeout 15 "$@"
}

provider_requirements() {
    cat <<'TXT'
AWS App Runner needs:
  - aws cli v2, authenticated             aws sts get-caller-identity
  - an ECR repository                     aws ecr create-repository --repository-name catalogue-api
  - an access role App Runner can assume to pull from ECR
      (AWSAppRunnerServicePolicyForECRAccess attached)
  - an instance role for the running task, allowed to read the secret
  - the database URL in Secrets Manager   aws secretsmanager create-secret \
                                            --name catalogue-database-url --secret-string "$DATABASE_URL"

  And one design decision, not a credential: App Runner has no per-revision
  URL, so verifying before promoting means a second service. Set
  AWS_STAGING_SERVICE and expect to pay for two.
TXT
}

provider_preflight() {
    local failures=0
    for pair in "AWS_REGION:${REGION}" "AWS_ACCOUNT_ID:${ACCOUNT}" \
                "AWS_ECR_REPOSITORY:${AWS_ECR_REPOSITORY:-}" \
                "AWS_ACCESS_ROLE_ARN:${AWS_ACCESS_ROLE_ARN:-}" \
                "AWS_INSTANCE_ROLE_ARN:${AWS_INSTANCE_ROLE_ARN:-}" \
                "AWS_DATABASE_URL_SECRET:${DATABASE_URL_SECRET}"; do
        [[ -n "${pair#*:}" ]] || { echo "  missing: ${pair%%:*} is not set in deploy/target.env" >&2; ((failures++)); }
    done
    command -v aws >/dev/null || { echo "  missing: aws cli is not on PATH" >&2; ((failures++)); }
    if command -v aws >/dev/null && ! _aws sts get-caller-identity >/dev/null 2>&1; then
        echo "  missing: aws cli is not authenticated — run 'aws configure' or set AWS_PROFILE" >&2
        ((failures++))
    fi
    ((failures == 0))
}

# --- App Runner's shape, which is not Cloud Run's -----------------------------

# App Runner sells fixed cpu/memory pairs rather than arbitrary values, so
# service.conf's numbers have to be mapped and some of them cannot be
# expressed at all. 1 vCPU with 512Mi is one of those: the smallest memory
# App Runner will pair with a full vCPU is 2 GB. Silently rounding up would
# quadruple the memory bill without anyone deciding to, so this refuses and
# says what the options are.
_apprunner_size() {
    local cpu="${CPU}" mem="${MEMORY}"
    case "${cpu}:${mem}" in
        0.25:512Mi) APPRUNNER_CPU="0.25 vCPU"; APPRUNNER_MEMORY="0.5 GB" ;;
        0.25:1Gi)   APPRUNNER_CPU="0.25 vCPU"; APPRUNNER_MEMORY="1 GB" ;;
        0.5:1Gi)    APPRUNNER_CPU="0.5 vCPU";  APPRUNNER_MEMORY="1 GB" ;;
        1:2Gi)      APPRUNNER_CPU="1 vCPU";    APPRUNNER_MEMORY="2 GB" ;;
        2:4Gi)      APPRUNNER_CPU="2 vCPU";    APPRUNNER_MEMORY="4 GB" ;;
        *)
            cat >&2 <<TXT
service.conf asks for CPU=${cpu} MEMORY=${mem}, which App Runner does not sell.

It offers fixed pairs, and the smallest memory it will pair with a full vCPU
is 2 GB. Pick one of these in service.conf, or override CPU/MEMORY per target:

    0.25 vCPU  with  512Mi or 1Gi
    0.5  vCPU  with  1Gi
    1    vCPU  with  2Gi, 3Gi or 4Gi
    2    vCPU  with  4Gi or 6Gi

Rounding this up silently would multiply the memory bill without anyone
choosing to, so it is a refusal rather than a default.
TXT
            return 1
            ;;
    esac
}

# What this target cannot honour, said out loud rather than discovered later.
provider_notes() {
    cat <<TXT

App Runner cannot honour two settings from service.conf:

  REQUEST_TIMEOUT_SECONDS=${REQUEST_TIMEOUT_SECONDS}
      There is no per-service request timeout. App Runner's is fixed at 120s.
      That matters here beyond tidiness: the 20s timeout on Cloud Run is what
      caps the cost of a client holding a connection open, which is how this
      service came to be billed around the clock. On App Runner that ceiling
      is 120s instead, so a held connection costs six times more.

  MAX_INSTANCES=${MAX_INSTANCES}
      Set through a separate AutoScalingConfiguration resource rather than on
      the service. Create one and pass its ARN as AWS_AUTOSCALING_ARN, or App
      Runner uses its default of 25 — which is a much larger ceiling than this
      service is sized for.
TXT
}

_service_arn() {
    _aws apprunner list-services --region "${REGION}" \
        --query "ServiceSummaryList[?ServiceName=='$1'].ServiceArn" --output text 2>/dev/null
}

_service_url() {
    local arn; arn="$(_service_arn "$1")"
    [[ -n "${arn}" && "${arn}" != "None" ]] || return 1
    printf 'https://%s' "$(_aws apprunner describe-service --region "${REGION}" \
        --service-arn "${arn}" --query 'Service.ServiceUrl' --output text)"
}

# App Runner returns from create/update before the service is serving, and the
# cli has no waiter for it, so this polls. Without it the smoke test runs
# against a service still deploying and fails for the wrong reason.
_wait_running() {
    local arn="$1" status deadline=$((SECONDS + 900))
    while ((SECONDS < deadline)); do
        status="$(_aws apprunner describe-service --region "${REGION}" \
            --service-arn "${arn}" --query 'Service.Status' --output text 2>/dev/null)"
        case "${status}" in
            RUNNING) return 0 ;;
            CREATE_FAILED|DELETE_FAILED) echo "service entered ${status}" >&2; return 1 ;;
        esac
        sleep 10
    done
    echo "timed out waiting for ${arn} to reach RUNNING (last status: ${status:-unknown})" >&2
    return 1
}

_source_configuration() {
    # Built by python rather than by string interpolation.
    #
    # API_MCP_ALLOWED_HOSTS is itself a JSON array, so embedding it as a JSON
    # *string* means escaping every quote inside it. Interpolating it directly
    # produces a document that parses as something else entirely, and the first
    # attempt here reached for bash's ${var@Q}, which is shell quoting rather
    # than JSON quoting — and is a bash 4.4 feature besides, on a machine whose
    # /bin/bash is 3.2.
    local image="$1" secret_arn
    secret_arn="$(_aws secretsmanager describe-secret --region "${REGION}" \
        --secret-id "${DATABASE_URL_SECRET}" --query 'ARN' --output text)" || return 1
    [[ -n "${secret_arn}" && "${secret_arn}" != "None" ]] || {
        echo "secret '${DATABASE_URL_SECRET}' not found in ${REGION}" >&2
        return 1
    }
    IMAGE_ID="${image}" SECRET_ARN="${secret_arn}" PORT="${CONTAINER_PORT}" \
    HOSTS="${API_MCP_ALLOWED_HOSTS}" ACCESS_ROLE="${AWS_ACCESS_ROLE_ARN}" \
    python3 -c '
import json, os
print(json.dumps({
    "ImageRepository": {
        "ImageIdentifier": os.environ["IMAGE_ID"],
        "ImageRepositoryType": "ECR",
        "ImageConfiguration": {
            "Port": os.environ["PORT"],
            "RuntimeEnvironmentVariables": {"API_MCP_ALLOWED_HOSTS": os.environ["HOSTS"]},
            "RuntimeEnvironmentSecrets": {"API_DATABASE_URL": os.environ["SECRET_ARN"]},
        },
    },
    "AutoDeploymentsEnabled": False,
    "AuthenticationConfiguration": {"AccessRoleArn": os.environ["ACCESS_ROLE"]},
}))'
}

_instance_configuration() {
    APPRUNNER_CPU="${APPRUNNER_CPU}" APPRUNNER_MEMORY="${APPRUNNER_MEMORY}" \
    ROLE="${AWS_INSTANCE_ROLE_ARN}" python3 -c '
import json, os
print(json.dumps({
    "Cpu": os.environ["APPRUNNER_CPU"],
    "Memory": os.environ["APPRUNNER_MEMORY"],
    "InstanceRoleArn": os.environ["ROLE"],
}))'
}

_create_or_update() {
    local name="$1" image="$2" arn
    _apprunner_size || return 1
    arn="$(_service_arn "${name}")"

    if [[ -n "${arn}" && "${arn}" != "None" ]]; then
        _aws apprunner update-service --region "${REGION}" --service-arn "${arn}" \
            --source-configuration "$(_source_configuration "${image}")" \
            --instance-configuration "$(_instance_configuration)" \
            >&2 || return 1
    else
        _aws apprunner create-service --region "${REGION}" --service-name "${name}" \
            --source-configuration "$(_source_configuration "${image}")" \
            --instance-configuration "$(_instance_configuration)" \
            --health-check-configuration '{"Protocol":"HTTP","Path":"/live","Interval":10,"Timeout":5,"HealthyThreshold":1,"UnhealthyThreshold":5}' \
            ${AWS_AUTOSCALING_ARN:+--auto-scaling-configuration-arn "${AWS_AUTOSCALING_ARN}"} \
            >&2 || return 1
        arn="$(_service_arn "${name}")"
    fi
    _wait_running "${arn}" || return 1
    printf '%s' "${arn}"
}

# --- the contract -------------------------------------------------------------

provider_build() {
    local image="${REGISTRY}:$1"
    _aws ecr describe-repositories --region "${REGION}" \
        --repository-names "${AWS_ECR_REPOSITORY}" >/dev/null 2>&1 || {
        echo "ECR repository '${AWS_ECR_REPOSITORY}' does not exist in ${REGION}" >&2
        return 1
    }
    _aws ecr get-login-password --region "${REGION}" \
        | docker login --username AWS --password-stdin "${REGISTRY%%/*}" >&2 || return 1
    # linux/amd64 explicitly: App Runner runs x86, and a build on an Apple
    # Silicon laptop defaults to arm64 and fails at start-up with an exec
    # format error that says nothing about architecture.
    docker build --platform linux/amd64 -f docker/api.Dockerfile -t "${image}" . >&2 || return 1
    docker push "${image}" >&2 || return 1
    printf '%s' "${image}"
}

# App Runner has no per-revision URL, so the candidate is a second service.
# It costs what a second service costs; that is the price of verifying before
# promoting on this platform.
provider_deploy_candidate() {
    _create_or_update "${AWS_STAGING_SERVICE}" "$1" >/dev/null || return 1
    provider_candidate_url
}

provider_candidate_url() { _service_url "${AWS_STAGING_SERVICE}"; }

provider_promote() {
    # The same image the candidate was verified with, deployed to the
    # production service. Nothing is rebuilt between verifying and promoting.
    _create_or_update "${AWS_SERVICE}" "${IMAGE}" >/dev/null
}

provider_url() { _service_url "${AWS_SERVICE}"; }

provider_rollback_hint() {
    printf 'aws apprunner update-service --service-arn $(aws apprunner list-services --query "ServiceSummaryList[?ServiceName==%s].ServiceArn" --output text) --source-configuration <previous image>' \
        "'${AWS_SERVICE}'"
}
