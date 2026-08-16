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

### What AWS and Azure would need

`aws.sh` and `azure.sh` exist and will tell you what they need — requirements
and preflight are implemented, so `DEPLOY_PROVIDER=aws scripts/deploy.sh
--check` is a real answer. Their deploy functions refuse rather than guess.

That is deliberate. Nothing in them has been run against an AWS account or an
Azure subscription, and a deploy script nobody has executed is worse than none:
it reads as capability and fails on first contact, with an image already built.
What is written down is checkable; what is not written is honestly absent.

Beyond credentials, one real difference each:

- **AWS App Runner has no per-revision URL.** There is no equivalent of a Cloud
  Run revision tag, so a candidate that serves no traffic has to be a second
  service — hence `AWS_STAGING_SERVICE`, and two services' worth of cost.
- **Azure Container Apps has revision labels**, which do what a Cloud Run tag
  does, so the staging step maps across directly. It is the closest of the
  three.

And one that applies to any new target: `API_MCP_ALLOWED_HOSTS` must name every
hostname the service answers on, including the candidate's. The MCP transport
refuses anything else with 421, and the SDK supports wildcards for ports but
not for hosts. A new hostname that nobody adds is an MCP endpoint that is
quietly broken there and nowhere else — which is exactly how the second of
Cloud Run's two URLs went unnoticed.

The container itself needs nothing: one Dockerfile, configuration by
environment variable, and no dependency on any cloud API at runtime.
