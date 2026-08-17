# TASKS

Status as of 2026-08-17.

## Done

**Demo stack** — runs on `temporalio/auto-setup` 1.27.4 (customer's version), every
image tag parameterised. `make validate` passes **33/33, 0 warnings**.

**Chaos scenarios (7)** — backlog, failures, orphan queue, slot saturation, worker
blackout, **stuck workflows**, **non-determinism**. The last two were the gaps:
`chaos-stuck` is the only scenario where the dashboards stay green *and that is
the finding*; `chaos-nde` proves the NDE alert fires end to end.

**Signals** — metrics (Prometheus/Tally), logs (Loki + Alloy), traces (OTel →
Tempo), profiles (Pyroscope + pprof). Collector-side attribute normalisation gives
`temporal.workflow.id` etc. for every SDK without app changes.

**Bundles** — `production/`, `cloud/`, `app-team/`, each with distinct prefixes and
no cross-bundle alert-name collisions.

**`monitor/`** — all six steps. Visibility-polling duration SLIs, recording rules,
alerts, dashboard row M, runbook. Proven against `chaos-stuck`: `0 → 5`
over-budget executions while no other alert fired.

**`terraform/`** — 3 modules, 3 examples, 4 design patterns. All pass
`init` + `validate` against provider schema v1.7.0.

**Distribution readiness** — MIT licence, "community, not Temporal-supported"
statement, `make verify-sdk-labels`, durable volumes, error threshold aligned to
99.9%.

## In flight

**`terraform/modules/self-serve-observability/`** — INCOMPLETE, uncommitted.

A module letting application teams provision their own Grafana folder, dashboard,
alerts and paging from ~20 lines of tfvars, so the platform team is not the
bottleneck for everyone else's observability.

- Written: `main.tf` — folder, permissions, contact point, 3 alert rules
  (schedule-to-start, workflow failure ratio, non-determinism), dashboard resource.
- **Missing: `variables.tf`, `outputs.tf`, `dashboard.json.tftpl`.**
- Does not validate yet — `main.tf` references variables and a template that do
  not exist.

Design decision already made and worth keeping: the module **never** creates a
`grafana_notification_policy`, because that resource overwrites the entire tree
and a per-team copy would make each team's apply erase every other team's
routing. Rules route directly via `notification_settings.contact_point`.

## Next

1. **Finish the self-serve module** — the three missing files, then validate.
2. **`terraform plan` against a real Cloud account.** Everything in `terraform/`
   validates and **nothing has been applied**. Validation proves the config is
   well-formed and correctly typed, not that the API accepts a given combination.
   This is the single biggest gap between "correct" and "trustworthy".
3. **Verify the Grafana log→trace deep link in a browser.** The TraceQL query is
   verified; the deep-link URL format is Grafana-version-sensitive and unclicked.
4. **Confirm the NDE label on non-Go SDKs.** `failure_reason` is verified on Go +
   tally only. If the customer's teams run Java/TS/Python, that critical alert may
   be silently dead — `make verify-sdk-labels` is the tool, but it needs running
   on their workers.
5. **Burn-rate ladder for duration SLOs** (optional). Not computable from the
   monitor's point-in-time gauges; would need short-window closed counts on the
   fast loop, roughly doubling Visibility query volume. Deliberate trade, recorded
   in `monitor/DESIGN.md`.

## Requested: AWS integration

Raised 2026-08-17. Not started — scoped here so it can be prioritised.

**AWS Simple Workflow Service (SWF) migration.** SWF is Amazon's legacy workflow
service and Temporal is the common migration target. What would earn its place:

- A comparison of the execution models — SWF deciders vs Temporal Workflows,
  activity task lists vs task queues, and where the semantics genuinely differ
  rather than just renaming.
- The observability delta specifically: SWF users are used to CloudWatch metrics
  and have no equivalent of schedule-to-start or sync match rate. Mapping what
  they lose and what they gain is the part this repo is well placed to write.
- A migration-shaped chaos scenario, if one is meaningful.

**AWS as a platform**, which is the broader and probably more useful half:

- **EKS** — Worker deployments, HPA on schedule-to-start rather than CPU (CPU is
  the wrong signal for a poller), and the Alloy DaemonSet pattern that replaces
  this repo's compose-specific Docker-socket log collection.
- **Lambda** — serverless Workers. Already covered in `demo/docs/`; worth
  revisiting against the current Serverless Workers docs.
- **S3 export sinks** — `temporalcloud_namespace_export_sink` supports S3 with
  `aws_account_id`, `bucket_name`, `region`, `role_name` and optional `kms_arn`.
  Workflow history archival for audit and compliance, provisionable in Terraform
  today.
- **IRSA / IAM roles for service accounts** — how Workers authenticate to AWS
  services without static credentials.
- **PrivateLink** via `temporalcloud_connectivity_rule`, for customers who cannot
  route Temporal traffic over the public internet.
- **CloudWatch vs Prometheus** — many AWS shops will want metrics in CloudWatch.
  The OTel Collector already in `demo/` can fan out to both, which is the natural
  place to add it.

Suggested first slice: **S3 export sink + EKS Worker deployment**, since both are
concrete, both are provisionable in Terraform, and both are things a platform
team hits in week one rather than eventually.

## Open questions for the customer

- **Sampling strategy for traces.** `AlwaysSample` is right for the lab and wrong
  for production. Head sampling drops slow executions at the same rate as fast
  ones — and the slow ones are the reason you are looking.
- **Trace retention vs workflow duration.** A Workflow can outlive the trace
  backend's retention. Tempo here keeps 24h.
- **Who owns thresholds?** The self-serve module deliberately makes them
  platform-owned so a team must have a conversation to change them. Confirm that
  matches how they want to operate.
