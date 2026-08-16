# Deploying

```
cp deploy/target.env.example deploy/target.env   # pick a cloud, fill in its names
scripts/deploy.sh --check                        # what it needs, and what is missing
scripts/deploy.sh v1.2.5                         # build, verify, promote
```

`--check` prints the target's requirements and then verifies the account
behind them: CLI installed, authenticated, registry present, secret present,
service account real. It runs before every deploy too, because a missing
secret found after an image has been built is the same error discovered at the
most expensive moment.

A deploy builds the image, starts it on a URL that serves no traffic, runs the
smoke script against it, and promotes only if that passes. A failure leaves
production on the revision it was already serving and the candidate up at its
own URL for inspection.

## Layout

| File | Holds |
|---|---|
| `deploy/target.env` | **Where** — which cloud, and that cloud's names. Gitignored, per-developer, no secrets. |
| `deploy/service.conf` | **What** — cpu, memory, concurrency, request timeout, instance ceiling, allowed hostnames. Cloud-neutral. |
| `deploy/providers/<name>.sh` | **How** — that cloud's spelling, behind a fixed set of functions. |
| `scripts/deploy.sh` | The sequence, which is the same everywhere. |

## Why it is arranged this way

**The gate.** Running the smoke script by hand *after* a deploy tells you what
you already shipped. Running it against a candidate that serves no traffic
tells you whether to ship at all. This has already paid for itself: the first
staged deploy failed the MCP handshake with 421 and production never moved —
and the same fault turned out to be live on one of the two hostnames Cloud Run
publishes, unnoticed until a staging URL exercised it.

**Idempotence.** Every setting in `service.conf` is passed on every deploy, so
the running service is a function of that file and the version argument rather
than of the sequence of commands anyone has typed at it. Deploy the same
version twice and the second is a no-op beyond a new revision; deploy after
someone has nudged a setting by hand and it goes back. Before this file
existed, the request timeout and the MCP allowed-hosts list lived only in
deployed state — set by hand, absent from the repository, and gone the moment
the service was recreated.

The limit worth knowing: this puts back the settings it names. A setting added
out-of-band that nothing here mentions will survive. Adding a knob means adding
it to `service.conf` *and* to the provider, or it becomes drift that outlives
the next deploy.

## Adding a cloud

`scripts/deploy.sh` is not Google-specific. The sequence — build, start
unpromoted, verify, promote — is the same on Cloud Run, App Runner and
Container Apps, and the two things being verified (the container, the smoke
script) are already portable. What differs is spelling, and it lives in
`deploy/providers/<name>.sh`, selected with `PROVIDER=<name>`.

A provider defines five functions:

| Function | Contract |
|---|---|
| `provider_build <version>` | Build and push. Print **only** the image reference; send build chatter to stderr. |
| `provider_deploy_candidate <image>` | Start it taking no production traffic. Print its URL. |
| `provider_candidate_url` | Print the candidate URL for an existing deploy. |
| `provider_promote` | Move production traffic to the candidate. |
| `provider_url` | Print the production URL. |
| `provider_rollback_hint` | Print the command that undoes a promotion. |

`service.conf` holds the five knobs every container platform has — CPU,
memory, concurrency, request timeout, instance ceiling — so a provider maps
them rather than reinventing them.

### Finishing AWS

The code is written; what remains is provisioning and one config change, then a
first run somewhere it can fail harmlessly.

**Provision** (once, in the target account):

```bash
aws ecr create-repository --repository-name catalogue-api --region <region>

aws secretsmanager create-secret --name catalogue-database-url \
    --secret-string "$DATABASE_URL" --region <region>

# Two roles, and they are not interchangeable. The access role is what App
# Runner assumes to pull from ECR; the instance role is what the running
# container has, and it needs secretsmanager:GetSecretValue for the secret
# above. Giving one role both jobs is the usual first mistake.
aws iam create-role --role-name AppRunnerECRAccess \
    --assume-role-policy-document file://trust-apprunner-build.json
aws iam attach-role-policy --role-name AppRunnerECRAccess \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess

aws iam create-role --role-name CatalogueApiInstance \
    --assume-role-policy-document file://trust-apprunner-tasks.json
# then an inline policy allowing secretsmanager:GetSecretValue on that secret

# Optional but recommended: without it App Runner's instance ceiling is 25,
# far above what this service is sized for.
aws apprunner create-auto-scaling-configuration \
    --auto-scaling-configuration-name catalogue --max-size 3 --region <region>
```

**Change one thing in `service.conf`.** `CPU=1` with `MEMORY=512Mi` is not
purchasable on App Runner — the smallest memory it pairs with a full vCPU is
2 GB. For a service this size:

```
CPU=0.25
MEMORY=512Mi
```

The provider refuses rather than rounding up, so this is a hard stop, not a
warning.

**Fill in `deploy/target.env`** — region, account id, both role ARNs, the
secret name, and the two service names. Then:

```bash
DEPLOY_PROVIDER=aws scripts/deploy.sh --check
DEPLOY_PROVIDER=aws scripts/deploy.sh v1.2.7
```

**Expect the first run to fail**, and prefer it to fail on a throwaway service
name. In rough order of likelihood: an IAM permission the roles do not carry;
the health check failing because `/live` is reached before the database is; the
wait loop timing out, since App Runner's first create is slower than a Cloud
Run revision. None of these are guesses about the code — they are the parts
that could not be exercised without an account.

**Then add the hostnames.** App Runner gives each service its own
`*.awsapprunner.com` name, and both the production and staging services need
theirs in `API_MCP_ALLOWED_HOSTS` or MCP answers 421 there and nothing else
explains why. This is the step most likely to be forgotten, because REST keeps
working perfectly while it is wrong.

### AWS

`aws.sh` is implemented and **has never been run against an AWS account**. Read
it as a careful first draft: the shapes come from the App Runner API, but the
first real run will find something, and it should happen somewhere it can fail
harmlessly.

Likely first failures, in order: an IAM permission the roles do not carry; the
health check failing because `/live` is reached before the database is; and the
wait loop timing out, because App Runner's first create takes longer than a
Cloud Run revision does.

Three things App Runner does differently, and none is a translation:

- **No per-revision URL.** No equivalent of a Cloud Run revision tag, so a
  candidate serving no traffic has to be a second service. `AWS_STAGING_SERVICE`
  is that, and it costs what a second service costs.
- **No request timeout.** App Runner's is fixed at 120s. The 20s ceiling here
  is what caps the cost of a client holding a connection open — the thing that
  had this service billed around the clock — and it cannot be carried across.
  A held connection on App Runner costs six times more.
- **Fixed cpu/memory pairs.** `CPU=1` with `MEMORY=512Mi` is not purchasable:
  the smallest memory App Runner pairs with a full vCPU is 2 GB. The provider
  refuses and lists the valid pairs rather than rounding up, because rounding
  would multiply the memory bill without anyone choosing to.

The first two are printed by `provider_notes` on every deploy. The third stops
the deploy.

### Azure

`azure.sh` still implements requirements and preflight only. Container Apps is
the closest fit of the three — revision labels do what a Cloud Run revision tag
does — so it is the smaller job of the two remaining.

### Anything new

`API_MCP_ALLOWED_HOSTS` must name every hostname the service answers on,
including the candidate's. The MCP transport refuses anything else with 421,
and the SDK supports wildcards for ports but not for hosts. A hostname nobody
adds is an MCP endpoint quietly broken there and nowhere else — which is how
the second of Cloud Run's two URLs went unnoticed.

The container itself needs nothing: one Dockerfile, configuration by
environment variable, and no dependency on any cloud API at runtime.
