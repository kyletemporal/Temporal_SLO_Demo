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

## Added 2026-09-26

**AI agent observability.** A durable agent loop in the Go demo app
(`demo/app/agent_workflow.go`) with three providers — Anthropic, OpenAI, and an
**offline deterministic mock that needs no API key and no egress** — plus an
optional LangChain/Python worker on its own task queue. Both emit ONE contract,
the OTel GenAI semantic conventions (`gen_ai.client.token.usage`,
`gen_ai.client.operation.duration`, `gen_ai.system`, …), so a single board
covers three SDKs in two languages. Board:
`grafana/dashboards/custom/ai-agent-observability.json` (16 panels), rules:
`prometheus/agent-rules.yml` (9 recording, 6 alerts). Written up in
[`demo/docs/AI-AGENT-OBSERVABILITY.md`](demo/docs/AI-AGENT-OBSERVABILITY.md).

The point of the board is the three failures that **move no Temporal metric**,
because none of them is an error: an agent that loops to its cap returns a
*successful* Workflow that answered nothing; one whose prompt cache stopped
hitting costs 3x at identical latency; one that quietly stopped calling a tool
looks perfect everywhere else. `make agent-runaway` demonstrates the first with
every golden-signals panel flat.

**OTel metrics pipeline**, alongside the existing Tally path rather than
replacing it. The two exporters emit **different names for the same Temporal
metrics**, so consolidating would rename every series the existing rules and
dashboards are built on. New namespace only: OTLP in, prometheus exporter on
`:8889`, scraped as `otel-collector-genai`, with four constant labels dropped
at scrape time.

**Worker scale harness.** `scripts/scale-benchmark.sh` + `k6/09-scale-sweep.js`
sweep the Worker count under a fixed **open-loop** offered load and write a CSV
and a report. Board: `grafana/dashboards/custom/worker-scale.json`. Methodology
in [`demo/docs/SCALE-TESTING.md`](demo/docs/SCALE-TESTING.md).

It produced a **negative result, which is the more useful kind**: throughput did
not scale (73.8/s → 70.1/s across a 4x fleet), slots were never the constraint
(33 of 40 free at the worst sampled moment), k6 delivered 79.99999/s with zero
dropped iterations — and schedule-to-start p99 *rose* from 0.44s to 2.84s as
Workers were added. Sync match rate of 0.17–0.30 fingerprints the real
bottleneck: persistence, on a laptop running one Postgres behind one
all-in-one Temporal container. **An HPA on schedule-to-start would have scaled
up in response, made it worse, and scaled up again** — which is the whole
argument for measuring before enabling one.

Three PromQL traps found while building it, all of which produced *wrong*
numbers rather than missing ones: `temporal_activity_execution_latency_count`
has zero live series (it is `_seconds_count`); `min_over_time()` over an
aggregation needs `[60s:15s]` subquery syntax, and the parse error reads as a
harmless `n/a`; and `temporal_worker_task_slots_available` counts Temporal's own
internal Workers at 1000 slots per internal queue — it reported **8110 free
slots for a fleet that had 40**. All three are documented in SCALE-TESTING.md §7.

**`validate.sh` extended** — the two new scrape jobs, the two GenAI rule groups,
the `gen_ai_*` metric families, and a new §10 that runs the Go↔Python contract
parity test (6 cases: cost maths, error taxonomy, instrument names, attribute
names). Two new gates keep it honest rather than red-by-default: `gen_ai_*`
panels WARN instead of FAIL until an agent has run, and container-CPU queries
WARN because this stack deliberately runs no cAdvisor. Gated on the *query*,
not on the panel title, so the slot-utilisation query sharing that panel still
has to return data.

**`make agent-py-test` now works without the OTel Python packages installed** —
it falls back to running the parity test inside the langchain image, which the
image now carries. It failed with `ModuleNotFoundError` on any host that had
not installed them, which is most hosts, since the whole point of that image is
that nobody has to.

## Verified live 2026-09-28

The agent + scale work is now verified against a running stack, not just
statically. 14 containers up including the new `agent-worker`; `make validate`
**46 passed, 0 failed, 1 warning**; all 11 Prometheus rule groups healthy
(including `genai-agent-recording` 9 and `genai-agent-alerts` 6); the Go↔Python
parity test passes **6/6 inside the langchain image**, exercising the fallback
path added for hosts without the OTel Python packages.

Panel coverage, measured after `make agent-demo` (offline mock, no API key):
**18 of 19** queries on the AI agent board return data, and **16 of 17** on
worker-scale. Each board has exactly one empty query and both are empty by
design — "Unpriced models", where a row would mean cost is being
under-reported, and the container-CPU series on "Slot utilisation vs CPU",
which needs a cAdvisor this stack deliberately does not run.

**The first live run failed 23 panels, and the dashboards were not at fault.**
`validate.sh` resolved exactly two template variables by name — `$namespace`
and `$task_queue` — so the moment boards arrived using other names it sent
Prometheus queries with the dollar sign still in them. The agent board's
`service_name=~"$service"` matched nothing and all 18 panels read as NO DATA;
worker-scale's `[$__range:]` is not parseable PromQL and came back HTTP 400.
Both boards were correct in Grafana the whole time. A validator that accuses
working panels is worse than one that misses a broken one, so the resolver is
now generic — macros plus a regex that turns any remaining `$var` in a label
matcher into match-all, the same rule `tools/check_dashboard_queries.py`
already used. The two are now in step, and were cross-checked against each
other on this run.

**The scale harness ran clean, and its report contradicted itself.** A
`make scale-benchmark-quick` sweep (1, 2 Workers @ 40/s) confirmed the three
PromQL fixes work live — `activities_s=105.24` and `min_slots=3` where the
buggy version produced `n/a` and `8110`. But the generated report then said
*"an autoscaler would make this worse"* and, four lines later,
*"schedule-to-start is the right metric to autoscale on for this workload"*.
The two narrative branches keyed on different columns and never consulted each
other. Fixed: the schedule-to-start branch now reads the throughput verdict
first, and the falling-S2S-without-rising-throughput case gets its own text
explaining why that pattern is the one an HPA handles worst. Both branches
re-verified, the second against a synthetic CSV.

The run itself is a good teaching case and is now written up in
SCALE-TESTING.md: the second Worker halved queue wait (0.289s → 0.171s) and
lifted sync match from 0.39 to 0.62 while moving throughput by 0.1/s. Better
at picking work *up*, no better at getting work *done*.

**`tools/check_dashboard_queries.py` was calling a healthy panel a bug.** It
sends every query to Prometheus, so the golden-signals board's one Loki panel
came back HTTP 400 and was printed under "these are bugs, not idle panels" —
the same class of error as the validator above, in the opposite direction. It
now skips non-Prometheus targets and reports the count. With that and the
resolver fix, the two checkers agree on every board, cross-checked on this run.

Also worth knowing, because it cost a diagnosis: **`shardinfo_immediate_queue_lag`
and `shardinfo_scheduled_queue_lag` do not exist until the history service has
completed a shard-info update cycle.** On a freshly started stack the "Shard
queue lag p95" panel on Full Overview reads as broken; ~10 minutes later the
same query returns 51 and 40 series. The server was emitting them on
`/metrics` before Prometheus had them, which is what ruled out a scrape-config
drop.

## Added 2026-09-29 → 2026-10-14

**Merged to main:** #5 (AI agent observability), #6 (worker scale harness),
#9 (self-hosted Temporal 1.28 on EKS + RDS PostgreSQL, `aws/eks-selfhosted/`,
with `scripts/cloudshell.sh` for a no-install deploy from AWS CloudShell).

**EKS self-hosted (`aws/eks-selfhosted/`).** Terraform (VPC, EKS 1.36, RDS
PostgreSQL 16) and Helm (chart 0.67.0, server 1.28.4, kube-prometheus-stack
with `production/` rules and dashboards) are kept as **separate layers** on
purpose. History shards default to 512, and `deploy.sh` refuses to change them
on an existing cluster. The Helm layer was verified end to end on kind against
a Postgres set up like RDS. **Never applied to AWS**, because no credentials
were available. Issue #8 stays open until the first real apply.

**Worker tuning lab (`demo/lab/`, PR #10, open).** `make lab` serves
http://localhost:8095, a live UI over real Go and Python Workers. Pollers,
slots, sticky cache, CPUs and the Activity profile can all change at runtime.
A diagnosis line is pinned by `lab_test.go` to the cases measured on the live
stack: slot-, poller-, CPU-, GIL-, downstream- and **server-bound**, plus cache
thrash. Findings are in `demo/lab/README.md`. The two that matter most:
- **Poll latency separates poller-bound (2–16 ms) from server-bound (0.6–1.0 s).**
- **A server at 240% CPU was not the bottleneck.** Raising pollers from 4 to 16
  cut a 4.4 s wait to 84 ms.

`make lab-slow-db` / `make lab-fast-db` reproduce server-bound by capping
Postgres CPU.

## Pick up here

1. **Review and merge PR #10** (tuning lab). The branch `tuning-lab` has the
   server-bound check committed.
2. **First real EKS deploy** from AWS CloudShell: `bash cloudshell.sh up`. Check
   the vCPU quota first (it needs 12 or more). A passing `smoke-test.sh` closes #8.
   It costs about $1/hour until `bash cloudshell.sh down`.
3. **PR #7 (transfer prep) is still in draft.** Five transfer-placeholder decisions
   are waiting on their owners: support posture ×2, disclosure policy, code of
   conduct, CODEOWNERS.
4. **Validator fragility (existing, not from the lab).** The worker-scale
   "Offered load vs throughput" panel sums `temporal_workflow_failed_total`. That
   metric doesn't exist until some Workflow has failed, so a fresh stack fails
   `make validate` (37/1/6) until a failure happens. The fix is `or vector(0)`
   on that term, in `tools/generate_scale_dashboard.py`.
5. **Lab follow-ups:**
   - Kubernetes manifests, so the lab runs against the EKS cluster.
   - Workers per lane can only be scaled with `--scale`, not from the UI.
   - The Python lab Worker isn't in Prometheus. Its SDK exports to one
     destination, and here that's the lab.
6. **EKS follow-up:** deploy `monitor/` in-cluster. Two `production/` recording
   rules stay empty without it.

Local state: the demo stack is **stopped** (containers kept). Postgres carries
`--cpus 18` from `make lab-fast-db`, which is no limit in practice; it goes away
on the next recreate.

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
5. ~~**Rebuild images once go1.25.13 is published**~~ Done 2026-10-16 the only
   way that worked: builders moved to Go 1.26 (every fixed `x/net` release
   requires it) and `x/net`/`grpc` bumped. `govulncheck` with the images'
   go1.26.9: 0 reachable in `demo/app` and `monitor`. See `SECURITY.md` P8.
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
