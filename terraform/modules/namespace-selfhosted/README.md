# namespace-selfhosted

Namespaces on demand for a **self-hosted** Temporal Service, defined as one map.

On Temporal Cloud use [`../namespace`](../namespace) instead — it is a real
provider resource and strictly better. This module exists because **there is no
Terraform provider for self-hosted Namespaces**: `temporalio/temporalcloud`
talks to the Cloud Ops API, which self-hosted does not have. So this drives the
`temporal operator` CLI.

Practices behind the defaults: [`../../docs/NAMESPACE-BEST-PRACTICES.md`](../../docs/NAMESPACE-BEST-PRACTICES.md).
Worked example: [`../../examples/05-namespaces-selfhosted`](../../examples/05-namespaces-selfhosted).

## Use

```hcl
module "namespaces" {
  source            = "../../modules/namespace-selfhosted"
  address           = "localhost:7233"
  default_retention = "168h"

  namespaces = {
    payments = {
      retention         = "720h"
      description       = "Payments team"
      owner_email       = "payments@example.com"
      data              = { team = "payments", tier = "1" }
      search_attributes = { CustomerTier = "Keyword" }
    }
    orders  = {}                                    # inherits default_retention
    sandbox = { retention = "24h", allow_destroy = true }
  }
}
```

Requires the `temporal` CLI on PATH and a reachable Frontend.

## What it does and does not give you

| | |
|---|---|
| Declarative fleet, idempotent apply | yes |
| **Drift detection** | yes — `data.external` reads live state every plan, `check` blocks report the difference |
| **Drift reconciliation** | yes, over two applies (see below) |
| A real resource graph | **no** — Terraform cannot roll back a half-finished CLI call |
| Field-level plan diffs | **no** — the plan shows a trigger change, not `retention: 24h -> 168h` |

## Four behaviours worth knowing

**Retention changes do not recreate the Namespace.** Existence and mutable
settings are two resources. `terraform_data.namespace` triggers on the **name
only**; `terraform_data.settings` triggers on retention/description/email. A
single resource would have meant editing retention destroyed the Namespace and
every history in it.

**Namespace creation is asynchronous.** `create` returns before the Frontend's
namespace cache refreshes, so an immediate follow-up call fails with
`Namespace X is not found` — which reads like the create failed. It did not.
The module polls until the Namespace resolves before returning.

**Out-of-band drift settles over two applies.** The trigger watches the observed
cluster state as well as your config, so a hand-edited retention forces the
update to re-run. Because data sources are read at plan time, the first apply
fixes the cluster and the second records the new observation. Verified:
`86400s → 604800s`, then clean.

**Search attributes are additive only.** The server has no delete for them, so
removing one from the map is silently a no-op. Names are permanent — typos too.

## Destroying

`allow_destroy` defaults to **false** and the destroy provisioner **fails**
rather than skipping:

```
REFUSING to delete namespace 'payments'.
This would destroy every Workflow history in it, and there is no undo.
```

A silent skip would drop it from state and orphan it — worse than either
outcome. To really delete: set `allow_destroy = true`, apply that on its own,
then destroy.

`prevent_destroy` cannot do this job — it only accepts a literal, so it cannot
be driven per-Namespace from a map.

## Outputs

`namespaces` (live state per Namespace), `names`, `monitor_namespaces`, and
`prometheus_namespace_regex` — feed the last two into `monitor/slo-config.yaml`
and the Prometheus rules instead of maintaining the list by hand.
