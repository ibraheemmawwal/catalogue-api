# Azure Container Apps.
#
# Requirements and preflight only, for the same reason as aws.sh: none of this
# has been run against an Azure subscription.
#
# Of the three this is the closest fit. Container Apps has revision labels,
# which do what a Cloud Run revision tag does — give a new revision its own
# hostname while the old one keeps traffic — so the staging step maps directly
# rather than needing a second service.

SUBSCRIPTION="${AZURE_SUBSCRIPTION:-}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-}"
REGISTRY="${AZURE_REGISTRY:-}"
DATABASE_URL_SECRET="${AZURE_DATABASE_URL_SECRET:-}"
CANDIDATE_LABEL=staging

provider_requirements() {
    cat <<'TXT'
Azure Container Apps needs:
  - az cli with the containerapp extension    az extension add --name containerapp
  - a signed-in subscription                  az login
  - a resource group                          az group create -n <rg> -l <location>
  - an Azure Container Registry               az acr create -n <acr> -g <rg> --sku Basic
  - a Container Apps environment              az containerapp env create -n <env> -g <rg>
  - a managed identity that can pull from the registry (AcrPull) and read the
    secret
  - the database URL in Key Vault             az keyvault secret set --vault-name <kv> \
                                                --name catalogue-database-url --value "$DATABASE_URL"

  Revision labels are the staging mechanism: a labelled revision gets its own
  hostname while traffic stays where it is, which is what the verify-then-
  promote step needs. Remember to add that hostname to API_MCP_ALLOWED_HOSTS,
  or MCP answers 421 there and nothing else will tell you why.
TXT
}

provider_preflight() {
    local failures=0
    for pair in "AZURE_SUBSCRIPTION:${SUBSCRIPTION}" "AZURE_RESOURCE_GROUP:${RESOURCE_GROUP}" \
                "AZURE_REGISTRY:${REGISTRY}" "AZURE_ENVIRONMENT:${AZURE_ENVIRONMENT:-}" \
                "AZURE_IDENTITY:${AZURE_IDENTITY:-}" "AZURE_KEYVAULT:${AZURE_KEYVAULT:-}" \
                "AZURE_DATABASE_URL_SECRET:${DATABASE_URL_SECRET}"; do
        [[ -n "${pair#*:}" ]] || { echo "  missing: ${pair%%:*} is not set in deploy/target.env" >&2; ((failures++)); }
    done
    command -v az >/dev/null || { echo "  missing: az cli is not on PATH" >&2; ((failures++)); }
    if command -v az >/dev/null; then
        az account show >/dev/null 2>&1 || { echo "  missing: az is not signed in — run 'az login'" >&2; ((failures++)); }
        az extension show --name containerapp >/dev/null 2>&1 || {
            echo "  missing: the containerapp extension — run 'az extension add --name containerapp'" >&2
            ((failures++))
        }
    fi
    ((failures == 0))
}

_unimplemented() {
    cat >&2 <<TXT
Azure deployment is not implemented.

The configuration and preflight are here so the requirements are written down
and checkable, but nothing has been run against an Azure subscription.

To finish it, implement in this file:
  provider_build             az acr build, or docker build then az acr login && push
  provider_deploy_candidate  az containerapp update with --revision-suffix, then
                             label the new revision '${CANDIDATE_LABEL}' with no traffic
  provider_candidate_url     the labelled revision's fqdn
  provider_promote           az containerapp ingress traffic set --revision-weight <new>=100
  provider_url               the app's fqdn
  provider_rollback_hint     traffic set back to the previous revision

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
provider_rollback_hint() { printf '(not implemented for azure)'; }
