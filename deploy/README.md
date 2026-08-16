# Deploying

```
scripts/deploy.sh v1.2.3
```

Builds the image, starts it on a URL that serves no traffic, runs the smoke
script against it, and promotes only if that passes. A failure leaves
production on the revision it was already serving and the candidate up at its
own URL for inspection.

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

Only `gcp.sh` exists, and deliberately so: a deploy script that has never been
run against the thing it deploys is worse than no script, because it reads as
capability. Writing `aws.sh` and `azure.sh` blind would produce two files that
look finished and fail on first contact, at the least convenient moment.

What each needs, beyond translating the table above:

- **A registry and credentials.** ECR or ACR, and something that can push.
- **Secrets.** `API_DATABASE_URL` comes from Secret Manager here; the
  equivalents are AWS Secrets Manager and Azure Key Vault, and the mapping is
  per-platform.
- **An unpromoted URL.** This is the part that varies most. Cloud Run gives it
  free with a revision tag. App Runner has no equivalent, so it takes a second
  service; Container Apps has revision labels, which are close to Cloud Run's
  tags.
- **The hostname list.** `API_MCP_ALLOWED_HOSTS` must name every hostname the
  service answers on, including the candidate's. The MCP transport refuses
  anything else with 421, and the SDK supports wildcards for ports but not for
  hosts. Every new hostname is an entry, or MCP is quietly broken there.

The container itself needs nothing: one Dockerfile, configuration by
environment variable, and no dependency on any Google API at runtime.
