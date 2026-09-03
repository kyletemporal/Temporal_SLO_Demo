# TASKS

Status as of 2026-08-24.

## Done

**Demo stack** — runs on `temporalio/auto-setup` 1.27.4 (customer's version), every
image tag parameterised. `make validate` passes **37/37, 0 warnings**.

**Chaos scenarios (8)** — backlog, failures, orphan queue, slot saturation, worker
blackout, **stuck workflows**, **non-determinism**, **poller flood**. The last two were the gaps:
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

## Added 2026-08-24

**Tracing is reachable.** Five Explore deep links on the golden-signals board
(slow activities, failed spans, service graph, all traces, CPU profiles), as
header links and as a button panel, plus per-series data links. Each query
verified against Tempo/Prometheus/Pyroscope. **The root cause was not the
links**: `GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer` carries no `datasources:explore`,
so Grafana's route guard silently redirected *every* `/explore` link — including
the datasource's trace→logs and trace→profile links — to the home page. Fixed
with `GF_USERS_VIEWERS_CAN_EDIT`. `validate.sh` §9 now checks the permission and
re-runs every button's query; negative-tested against a clean Grafana.

**`SECURITY.md`** — 19 findings, no Critical, no committed secrets. Two High,
both demo-only and accepted with mitigations: the Docker socket in Alloy (`:ro`
does *not* make the Docker API read-only) and 14 unauthenticated published ports.
`govulncheck` against the toolchain the images actually build with: 6 reachable
stdlib vulns in `demo/app`, 4 in `monitor`, all fixed by rebuilding on go1.25.13.

**Nexus, self-hosted.** Callback dynamic config, port 7243 published, and
`scripts/tctl.sh` + `make ns-*` / `nexus-*` for Namespace and Endpoint CRUD.
Most of it was already on by default in 1.27.4. The trap `make nexus-doctor`
exists to catch: **Endpoint registration succeeds with no callback config at
all**, and only fails at first invocation.

**`terraform/modules/namespace-selfhosted/`** — Namespaces on demand from one
map, planned *and applied* against the live cluster. There is no provider for
self-hosted Namespaces, so it drives the CLI.

**`temporal-overview`** (the Grafana home dashboard) and
**`temporal-full-overview`** — a row-for-row rebuild of Grafana Cloud's Temporal
dashboard for self-hosted. Mapping in `docs/CLOUD-TO-SELFHOSTED.md`.

**`aws/k8s/karpenter/`** and **`aws/k8s/worker/hpa.yaml`** — node provisioning
against the Karpenter **v1** API, and a plain-HPA alternative to KEDA.

**Scenario 8 — poller flood.** The first chaos scenario that runs the fleet in
the OPPOSITE direction; 0-7 are all starvation shapes. Over-provision and poll
success rate collapses (0.9995 -> 0.6812) while sync match *improves* to 1.0000
and schedule-to-start falls to 0.0088s. `TemporalPollSuccessRateLow` is renamed
to `TemporalMatchingStarved` with schedule-to-start as a required second
condition — the old rule fired on a healthy cluster. Row P added to the
golden-signals board via `tools/add_poll_outcome_panels.py`.

`make validate` passes **37/37, 0 warnings**, stable across repeated runs.

## In flight

Nothing. The self-serve observability module listed here previously was
completed in `ad7d184` — all of `variables.tf`, `outputs.tf` and
`dashboard.json.tftpl` exist and the module validates.

Design decision worth keeping visible: that module **never** creates a
`grafana_notification_policy`, because that resource overwrites the entire tree
and a per-team copy would make each team's apply erase every other team's
routing. Rules route directly via `notification_settings.contact_point`.

## Next

1. **`terraform plan` against a real Cloud account.** Everything in
   `terraform/` validates and **nothing has been applied**. Validation proves
   the config is well-formed and correctly typed, not that the API accepts a
   given combination. Still the single biggest gap between "correct" and
   "trustworthy". (The *self-hosted* namespace module is now applied and
   verified, including drift detection — the Cloud modules are not.)
2. **Click one trace button in a browser.** Every query is verified server-side
   and the permission bug is fixed, but one thing cannot be checked without a
   browser: whether Grafana interpolates `${__from}` / `${__to}` / `${trace_slow}`
   inside a **text-panel** `href`. If it does not, the links still open Explore
   but land on the default time range instead of the dashboard's.
3. **Confirm the NDE label on non-Go SDKs.** `failure_reason` is verified on Go
   + tally only. If the customer's teams run Java/TS/Python, that critical alert
   may be silently dead — `make verify-sdk-labels` is the tool, but it needs
   running on their workers.
4. **Apply the `aws/` manifests to a real EKS cluster.** Karpenter and the HPA
   are written against verified APIs and parse cleanly; neither has been
   applied. The Karpenter setting most likely to need tuning is
   `consolidationPolicy` — watch `temporal_activity_execution_failed_total`
   after a scale-down.
5. **Rebuild images once go1.25.13 is published** to clear the 10 stdlib
   vulnerabilities in `SECURITY.md` P8. `golang:1.25-alpine` is a floating tag,
   so `docker compose build --no-cache` picks it up with no file change.
6. **Burn-rate ladder for duration SLOs** (optional). Not computable from the
   monitor's point-in-time gauges; would need short-window closed counts on the
   fast loop, roughly doubling Visibility query volume. Deliberate trade,
   recorded in `monitor/DESIGN.md`.

## Raised upstream

**`task_accepted_latency`** — filed as
[temporalio/temporal#11916](https://github.com/temporalio/temporal/issues/11916)
on 2026-09-03, from a question by Kevin Woo.

No metric covers RPC receipt -> history task durably committed. Every segment of
that path is instrumented and none of them start before task *generation*,
verified against `metric_defs.go` on `main` rather than assumed. The segments
cannot be summed — three services, three histograms, no shared exemplar.

Wider on Cloud: none of the `task_latency_*` family is exposed in any of the 57
documented `temporal_cloud_v1_*` metrics, so a Cloud customer has nothing between
frontend RPC latency and queue depth.

Full measurement and reasoning: [`docs/FR-task-accepted-latency.md`](docs/FR-task-accepted-latency.md).
Nothing to do here until upstream responds.

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

- **EKS** — ~~Worker deployments, HPA on schedule-to-start rather than CPU~~
  **DONE** (`aws/k8s/`): Deployment, PDB, IRSA, KEDA *and* plain-HPA autoscaling,
  plus Karpenter node provisioning. Still open: the **Alloy DaemonSet** pattern
  that replaces this repo's compose-specific Docker-socket log collection —
  which is also SECURITY.md finding D1, the highest-severity one in the repo.
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

**DONE 2026-08-17: S3 export sink + EKS Worker deployment** — see [`aws/`](aws).
Both validate; neither has been applied. Remaining AWS items above are still open,
with CloudWatch fan-out via the existing OTel Collector the most natural next one.

## Open questions for the customer

- **Sampling strategy for traces.** `AlwaysSample` is right for the lab and wrong
  for production. Head sampling drops slow executions at the same rate as fast
  ones — and the slow ones are the reason you are looking.
- **Trace retention vs workflow duration.** A Workflow can outlive the trace
  backend's retention. Tempo here keeps 24h.
- **Who owns thresholds?** The self-serve module deliberately makes them
  platform-owned so a team must have a conversation to change them. Confirm that
  matches how they want to operate.
