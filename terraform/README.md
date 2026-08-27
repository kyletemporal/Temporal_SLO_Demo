# Temporal Cloud with Terraform

Modules and worked examples for managing Temporal Cloud as infrastructure as
code, using the official [`temporalio/temporalcloud`](https://registry.terraform.io/providers/temporalio/temporalcloud/latest)
provider.

Built against **provider v1.7.0** and validated with `terraform validate`.

## What is here

| | |
|---|---|
| `modules/namespace` | A Namespace with the irreversible settings made explicit and the expensive ones deliberate |
| `modules/team-onboarding` | Namespace + workload identity + credential + human access, in one apply |
| `modules/observability` | A least-privilege metrics scraper for the **current** OpenMetrics endpoint |
| `modules/self-serve-observability` | A team's own Grafana folder, dashboard, alerts and paging from ~20 lines of tfvars |
| `modules/namespace-selfhosted` | **Self-hosted** Namespaces on demand from one map. No provider exists for these — it drives the `temporal operator` CLI. See [`docs/NAMESPACE-BEST-PRACTICES.md`](docs/NAMESPACE-BEST-PRACTICES.md) |
| `examples/01-namespace` | Single Namespace with search attributes |
| `examples/02-team-onboarding` | Onboarding a team end to end |
| `examples/03-observability` | Metrics access, and a ready-to-paste Prometheus scrape job |
| `patterns/` | Four design patterns for multi-team use — see [`PATTERNS.md`](PATTERNS.md) |

## Honest status

Every example passes `terraform init` and `terraform validate` against the real
provider schema. **None has been `terraform apply`-ed**, because that requires a
Temporal Cloud account. Validation proves the configuration is well-formed and
that every attribute exists and is correctly typed; it does not prove the API
accepts a given combination of values at apply time. Run `terraform plan` against
your own account before trusting any of it.

## Quick start

```bash
export TEMPORAL_CLOUD_API_KEY=<your key>   # never hardcode this in a .tf file
cd examples/01-namespace
terraform init
terraform plan
```

## Three findings from building this

**1. The docs are two major versions behind the provider.**
The published examples show `version = ">= 0.0.6"` and cover Namespaces, Users,
API keys and Nexus endpoints. v1.7.0 also has `metrics_endpoint`,
`namespace_export_sink`, `account_audit_log_sink`, `connectivity_rule`,
`custom_role`, `group`, `group_access`, `group_members` and `namespace_tags`.
Read the schema, not the tutorial:

```bash
terraform providers schema -json | jq '.provider_schemas[].resource_schemas | keys'
```

**2. `temporalcloud_metrics_endpoint` provisions the DEPRECATED endpoint.**
Its name makes it look like the obvious way to wire up metrics. It is not. Its
schema gives it away — it takes `accepted_client_ca` and returns a "Prometheus
metrics endpoint URI", which is the **mTLS PromQL endpoint**: deprecated
2026-04-02, closed to new users, and **disabled for everyone on 2026-10-05**.

The current path is the **OpenMetrics** endpoint at `metrics.temporal.io`,
authenticated with a Bearer API key from a `metricsread` service account. That is
what `modules/observability` builds by default; the deprecated resource is behind
a variable whose name says what it is.

**3. Block syntax from the docs does not compile.**
`certificate_filters` and `codec_server` are typed *attributes* in v1.7.0, not
blocks. `dynamic "codec_server" { }` fails with *"Blocks of type codec_server are
not expected here"*. `terraform validate` catches it in seconds, which is why
every example runs it.

## Relationship to the rest of this repo

`modules/observability` outputs a scrape config that feeds the
[`cloud/`](../cloud) bundle's rules and dashboards, and `modules/namespace`
provisions the custom search attributes that [`monitor/`](../monitor) queries for
duration SLOs. The three fit together: Terraform provisions the Namespace and the
credential, `cloud/` alerts on what Temporal reports, and `monitor/` answers the
duration question neither of them can.

See [`PATTERNS.md`](PATTERNS.md) for design patterns that show up once more than
one team uses Temporal Cloud: environment promotion, cross-team service
boundaries via Nexus, zero-downtime credential rotation, and split ownership
between a platform team and product teams.

See [`BEST-PRACTICES.md`](BEST-PRACTICES.md) for the operational rules these
modules encode and why each one exists.
