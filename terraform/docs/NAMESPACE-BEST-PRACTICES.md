# Namespace deployment — best practices

For platform teams provisioning Temporal Namespaces for other people's teams.

Every rule here is either a decision that **cannot be undone**, or one that
routinely gets made by accident and discovered during an incident.

---

## 1. The three things you cannot change later

Get these wrong and the fix is "create a new Namespace and migrate every
Workflow to it."

| | Why it is permanent |
|---|---|
| **The name** | There is no rename. Callers, task queues, dashboards, alert routes and IAM policies all key off it. |
| **Regions** (Cloud) | `regions` forces replacement on `temporalcloud_namespace`. |
| **Retention, retroactively** | You can *raise* retention any time — but it does not resurrect histories already deleted under the old value. |

So: **set retention explicitly at creation, always.** The server default is
**72h**. A team that assumes "about a month" finds out on the Monday after a
Friday incident, and by then the evidence is gone. Neither module in this repo
lets you create a Namespace without a retention, and the self-hosted one
defaults to 168h rather than inheriting the server's 72h.

### Naming

Pick a scheme before the first Namespace, because you are stuck with it:

```
<team>-<env>          payments-prod, payments-staging
```

Environment **in the name**, not just in a tag. The single most expensive
Namespace mistake is a Worker pointed at the wrong one, and `payments-prod` vs
`payments-staging` in a connection string is visible in a code review in a way
that a `tags = { env = "prod" }` three files away is not.

Constrain the character set. Both modules validate
`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$` — not because Temporal requires it, but
because anything else makes CLI quoting and Prometheus label matching fragile,
and you cannot rename your way out.

---

## 2. One Namespace per team, per environment

A Namespace is the isolation boundary that matters. It is where retention,
search attributes, rate limits and (on Cloud) billing attach.

**Split when** teams have different retention needs, different on-call, or
different compliance scope. Also split prod from everything else, always.

**Do not split** per microservice or per workflow type. Namespaces are not free
— each is a separate credential to rotate, dashboard row, alert route and
Nexus Endpoint registration — and Task Queues already give you routing
isolation inside a Namespace at no cost.

The signal you have split too finely: your platform team is the bottleneck for
creating them. That is what `terraform/modules/self-serve-observability` and
the map-based factory below are for.

---

## 3. Provision them as a fleet, not one at a time

Both modules in this repo take a **map**:

```hcl
module "namespaces" {
  source = "../../modules/namespace-selfhosted"   # or ../namespace for Cloud

  namespaces = {
    payments = { retention = "720h", search_attributes = { CustomerTier = "Keyword" } }
    orders   = { }                                 # inherits default_retention
    sandbox  = { retention = "24h", allow_destroy = true }
  }
}
```

Adding a Namespace is **one block in one file** — reviewable in a PR, diffable,
and impossible to do in three subtly different ways. A copied module block per
Namespace drifts within a quarter; a copied *directory* per Namespace drifts
within a week.

Keep one state file for the fleet. Per-Namespace state buys blast-radius
isolation that `prevent_destroy` and `allow_destroy` already give you, at the
cost of N backends to configure.

---

## 4. Make deletion require a deliberate act

**Deleting a Namespace deletes every Workflow history in it. There is no undo
and no export step.**

- **Cloud** (`modules/namespace`): `lifecycle { prevent_destroy = true }`. Note
  this only accepts a literal, so it cannot be driven per-Namespace from a map.
- **Self-hosted** (`modules/namespace-selfhosted`): `allow_destroy` per
  Namespace, defaulting to `false`. The destroy provisioner **fails** rather
  than skipping — a silent skip would drop it from state and orphan the
  Namespace, which is worse than either outcome.

Either way the intended workflow is: flip the flag in its own reviewed commit,
apply that, then destroy. Two humans, two changes.

Watch for the rename trap: `name` forces replacement, so a one-character typo
in a variable is a **destroy and recreate of production**. That is the real
reason the guard exists.

---

## 5. Declare search attributes at creation

Search attributes are how anything finds Workflows later — including the
Visibility-based duration SLOs in `monitor/`. Adding one is cheap; needing one
mid-incident and not having it is not.

**They are additive only.** The server has no delete (the CLI tells you to
contact support), so:

- Removing one from your tfvars is **silently a no-op**, not a removal. Both
  modules document this; neither can fix it.
- A typo in a name is permanent clutter.
- Adding a *new* attribute is safe and instant.

Declare the ones your queries and SLOs need on day one. `CustomerTier`,
`OrderPriority`, and whatever your runbooks search by.

---

## 6. Credentials: rotate by construction, not by procedure

On Cloud, `temporalcloud_apikey.token` is a real credential and **Terraform
state stores it in plaintext.** State access is credential access.

- Encrypt the state backend (`encrypt = true` on S3), enable locking, and
  restrict `s3:GetObject` on the state bucket to the CI role.
- Never run these configurations with a personal account that can also read
  the bucket.
- Use the two-slot pattern in `terraform/patterns/03-credential-rotation` so
  rotation is a variable flip rather than a runbook. A disabled key is still a
  credential sitting in state.
- Set `expiry_time`. A key with no expiry is a key nobody ever rotates.

Split ownership where the boundary is real:
`terraform/patterns/04-split-ownership` puts the platform team and each app
team in **separate states with separate credentials**, so a team's credentials
cannot modify the platform's configuration and cannot read every other team's
API key token out of shared state.

---

## 7. Wire the fleet into observability at creation time

A Namespace nobody is watching is worse than no Namespace, because it looks
like coverage.

Both modules output the fleet in the shapes the rest of this repo consumes:

```hcl
output "prometheus_namespace_regex"   # ^(orders|payments|sandbox)$
output "monitor_namespaces"           # for monitor/slo-config.yaml
```

Feed those in rather than maintaining the namespace list in the rules, the
monitor config and the dashboards separately — three hand-maintained copies
means the newest Namespace is unmonitored, and you find out when it breaks.

Every alert in this repo scopes to `namespace!~"temporal_system|system|_unknown_"`
for a measured reason: `absent(temporal_worker_task_slots_available)` **never
fires** because Temporal's own internal Workers keep emitting that metric under
`temporal_system`. 22 server-emitted series survive killing the entire
application fleet. Absence alerts must be scoped to *your* Namespaces.

---

## 8. Self-hosted: what has no Terraform provider

**There is no Terraform provider for self-hosted Temporal Namespaces.** The
`temporalio/temporalcloud` provider talks to the Cloud Ops API, which a
self-hosted cluster does not have.

`modules/namespace-selfhosted` drives the `temporal operator` CLI instead, and
the honest summary of that trade is:

- You get declarative fleet definition, idempotent apply, and real drift
  detection (`data.external` reads live state every plan; `check` blocks report
  the difference).
- You do not get a real resource graph. Terraform cannot roll back a
  half-finished CLI call.

Two behaviours that bit during development and are now handled — both worth
knowing even if you write your own tooling:

**Namespace creation is asynchronous.** `namespace create` returns success as
soon as the record is written, but the Frontend serves lookups from a cache
that refreshes on an interval. For several seconds the Namespace does not exist
as far as every other API call is concerned, and an immediate `namespace update`
fails with `Namespace X is not found` — which reads exactly like the create
failed. Terraform makes it worse by running the fleet in parallel. The module
polls until the Namespace is resolvable before returning.

**Detected drift must also be reconciled.** The first version triggered updates
only on config changes, so when someone changed retention by hand the `check`
block reported it and `apply` then did nothing. Detected-but-never-fixed trains
people to ignore the warning. The trigger now watches the observed cluster state
as well, so drift forces the update to re-run. Because data sources are read at
plan time, out-of-band drift settles over two applies — verified end to end:
86400s → 604800s, then clean.

---

## 9. Checklist

- [ ] Retention set explicitly, never inherited from the 72h server default.
- [ ] Environment in the Namespace **name**, not only in tags.
- [ ] Name matches a constrained charset — it can never be changed.
- [ ] One Namespace per team per environment; not per service.
- [ ] Fleet defined as a map in one file, one state.
- [ ] Destroy guarded (`prevent_destroy` / `allow_destroy = false`).
- [ ] Search attributes declared at creation — they cannot be removed.
- [ ] API keys have an `expiry_time`; state backend encrypted and locked.
- [ ] Namespace list feeds the Prometheus rules and monitor config from the
      module outputs, not by hand.
- [ ] Alerts scoped to your Namespaces, excluding `temporal_system`.
