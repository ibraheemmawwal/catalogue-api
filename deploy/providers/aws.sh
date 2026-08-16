# AWS App Runner.
#
# Requirements and preflight only. The deploy functions refuse rather than
# guess: nothing here has ever been run against an AWS account, and a deploy
# script that has not is worse than none, because it reads as capability and
# fails on first contact with an image already built.
#
# What is genuinely different, and why this is not a translation exercise:
# App Runner has no equivalent of a Cloud Run revision tag. There is no way to
# run a new version on its own URL while the old one keeps serving, so the
# staging step has to be a second service — which is why AWS_STAGING_SERVICE
# exists and AWS_SERVICE is not enough on its own.

REGION="${AWS_REGION:-}"
ACCOUNT="${AWS_ACCOUNT_ID:-}"
REGISTRY="${ACCOUNT:+${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${AWS_ECR_REPOSITORY:-}}"
DATABASE_URL_SECRET="${AWS_DATABASE_URL_SECRET:-}"

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
    if command -v aws >/dev/null && ! aws sts get-caller-identity >/dev/null 2>&1; then
        echo "  missing: aws cli is not authenticated" >&2
        ((failures++))
    fi
    ((failures == 0))
}

_unimplemented() {
    cat >&2 <<TXT
AWS deployment is not implemented.

The configuration and preflight are here so the requirements are written down
and checkable, but nothing has been run against an AWS account and a deploy
script nobody has executed is a liability rather than a feature.

To finish it, implement in this file:
  provider_build             docker build, then push to ${REGISTRY:-<ecr repo>}
  provider_deploy_candidate  create-or-update ${AWS_STAGING_SERVICE:-<staging service>},
                             return its https URL
  provider_candidate_url     the staging service's URL
  provider_promote           update ${AWS_SERVICE:-<service>} to the same image
  provider_url               the production service's URL
  provider_rollback_hint     update-service back to the previous image

The container needs no changes: one Dockerfile, configuration by environment
variable, and nothing Google-specific at runtime.
TXT
    return 1
}

provider_build() { _unimplemented; }
provider_deploy_candidate() { _unimplemented; }
provider_candidate_url() { _unimplemented; }
provider_promote() { _unimplemented; }
provider_url() { _unimplemented; }
provider_rollback_hint() { printf '(not implemented for aws)'; }
