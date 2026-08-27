# Grafana Cloud's Temporal dashboard → self-hosted

Grafana Cloud ships a "Temporal overview" dashboard with its Cloud Connections
integration. It is a good dashboard and **none of it works on a self-hosted
cluster.** This is the full mapping of what was translated, what was replaced,
and what was removed outright.

| | |
|---|---|
| Source | Grafana Cloud "Temporal overview", `dashboard.grafana.app/v2` |
| Result | `demo/grafana/dashboards/custom/temporal-full-overview.json` |
| Generator | `tools/generate_selfhosted_full.py` |
| Verified against | Temporal Server 1.27.4, Grafana 11.5.1, Go SDK — 49 of 59 queries return live data, the other 10 are empty by design and listed below |

---

## The four reasons a direct port fails

Each of these produces a **wrong or empty dashboard rather than an error**,
which is why they are worth stating before the table.

**1. Schema.** The source is the Grafana 12 dashboard schema
(`elements` / `RowsLayout` / `VizConfig`). Grafana 11.5.1 does not parse it —
no partial render, no message. The rebuild emits classic `schemaVersion 39`.

**2. Metric namespace.** Every Cloud query reads `temporal_cloud_v1_*`. Those
series exist only on Cloud's metrics endpoint. Ported unchanged you get a full
page of "No data", which on an overview reads as an outage.

**3. Counters versus gauges.** `temporal_cloud_v1_*` are **pre-computed
per-second gauges** — you `sum()` them and must never `rate()` them.
Self-hosted server metrics are **counters** and are meaningless without
`rate()`. Swapping only the metric name gives a number that is *wrong* rather
than absent. This is the failure mode most likely to survive review.

**4. Label names.** Three conventions collide, and mixing them yields an empty
panel with no hint why:

| Concept | Cloud | Self-hosted server | Go/Java SDK |
|---|---|---|---|
| Namespace | `temporal_namespace` | `namespace` | `namespace` |
| Task queue | `temporal_task_queue` | **`taskqueue`** | **`task_queue`** |
| Workflow type | `temporal_workflow_type` | **`workflowType`** | **`workflow_type`** |
| Activity type | — | **`activityType`** | **`activity_type`** |

Note `taskqueue` with no underscore on the server, `task_queue` with one on the
SDK, and camelCase on server activity metrics. All verified on a live cluster.

---

## Translated — same question, self-hosted metric

| Cloud panel | Cloud metric | Self-hosted |
|---|---|---|
| Open Workflows | `namespace_open_workflows` | `temporal_slo_running_executions` — from `monitor/`. **Nothing in Prometheus counts executions that have not ended**; that is why the monitor exists. |
| Successful workflows | `workflow_success_count` | `rate(workflow_success)` |
| Failed / Timeouts | `workflow_failed_count`, `workflow_timeout_count` | `rate(workflow_failed)`, `rate(workflow_timeout)`, plus `workflow_terminate` |
| Workflow latency p50/p95/p99 | `workflow_schedule_to_close_latency_p*` | `temporal_workflow_endtoend_latency_seconds_bucket` + `histogram_quantile` — Cloud pre-computes percentiles, self-hosted ships raw histograms |
| Activity end-to-end latency | `activity_schedule_to_close_latency_p*` | `activity_end_to_end_latency_bucket` |
| Activity failures | `activity_fail_count`, `activity_task_fail_count` | `temporal_activity_execution_failed_total` — **one metric, not two.** The Go SDK counts every failed attempt, so this is the "including retries" number; the retries-excluded panel has no equivalent |
| Poll success / sync / async / timeout | `poll_success_count`, `poll_success_sync_count`, `poll_timeout_count` | `poll_success`, `poll_success_sync`, **`poll_timeouts`** (plural — `poll_timeout` does not exist) |
| No poller tasks | `no_poller_tasks_count` | `no_poller_tasks` |
| Sync match rate | ratio of the two poll counts | same ratio, `poll_success_sync / poll_success` |
| Service requests / errors | `service_request_count`, `service_error_count` | `service_requests`, `service_error_with_type` (+ client-fault filter, see below) |
| Service latency by operation | `service_latency_p*{operation=...}` | `service_latency_bucket{operation=...}` + `histogram_quantile` |
| Pending requests | `service_pending_requests` | `service_pending_requests` — **the one that ports unchanged.** A gauge on both, so no `rate()` |
| Resource exhausted | `resource_exhausted_error_count` | `service_errors_resource_exhausted`, broken down by `resource_exhausted_cause` |

### One filter that is not in the Cloud original

Errors are filtered to **server faults only**. Client faults are not your
errors: Matching emits a steady ~0.39/s of `serviceerror_Canceled` at idle, and
counting it took measured availability to 98.77% and blew a 99.9% error budget
many times over while nothing was wrong.

Frontend latency likewise **excludes long-polls**. `PollWorkflowTaskQueue` and
`PollActivityTaskQueue` block up to 60s by design and are the highest-volume
operations. Included: 95.9% of requests "under 500ms". Excluded: 100%.

---

## Replaced — the question does not survive, the concern does

| Cloud row | Why it cannot port | What replaced it |
|---|---|---|
| **Usage & Quotas** (`action_limit`, `service_request_limit`, `operations_limit`) | Actions and per-namespace rate limits are Cloud **provisioning** constructs. Self-hosted has no quota — you are the capacity. | **Persistence and shards** row |
| **Billable Actions** (9 panels: `billable_action_count`, heartbeat ratio, retry ratio, 7d/30d totals) | Pure **billing**. A self-hosted cluster has no actions and no bill. | — |
| **Provisioned Capacity (TRU)** (6 panels: `action_on_demand_envelope_limit`, capacity utilisation) | TRU is a Cloud **purchasing** unit. | — |
| Task queue backlog (`approximate_backlog_count`) | No self-hosted gauge exists for queue depth. | **Sync match rate** + **tasks with no poller**, which measure the same thing (is the queue keeping up) from the server side, and lead rather than lag |
| Actions/sec | Billing unit. | **Frontend requests/sec** — request volume, explicitly *not* a cost proxy |

All three removed rows ask *"am I inside what I paid for?"* — a question
self-hosted does not have. The self-hosted version of the same worry is *"is my
infrastructure keeping up?"*, and the answer is almost always the datastore.
So the replacement row is **Persistence and shards**:

- `persistence_latency_bucket` p50/p95/p99, and p95 by operation
- `persistence_requests` against `persistence_errors`
- `shardinfo_immediate_queue_lag` / `shardinfo_scheduled_queue_lag` p95 — how
  far behind the History service's internal queues are. No Cloud equivalent at
  all, because Temporal operates the shards for you there. Self-hosted this is
  the earliest warning that History cannot keep up, and it moves well before
  user-visible latency does.

**Check persistence before scaling any Temporal service.** Most self-hosted
Temporal incidents are persistence incidents wearing a different hat, and
adding replicas against a saturated datastore makes it worse.

---

## Removed — no self-hosted equivalent, and no substitute

| Cloud panel | Why |
|---|---|
| **Replication lag** (`replication_lag_p*`) | Cloud multi-region high-availability namespaces. A single-cluster self-hosted deployment has no replication to lag. |
| **Schedules row** (`schedule_action_success_count`, `schedule_buffer_overruns_count`, `schedule_missed_catchup_window_count`, `schedule_rate_limited_count`) | **Not emitted on this deployment.** No `schedule_*` server metric appears in Prometheus on 1.27.4 with no Schedules in use. Rather than ship four permanently-dead panels, the row is omitted — add it back if your cluster runs Schedules and the metrics appear. |
| Workflow **cancel** and **continued-as-new** counts | The Cloud board plots both. Neither `workflow_cancel` nor `workflow_continued_as_new` was observable on this cluster. A panel querying a metric that may never exist is worse than an honest omission. |
| Per-namespace **limit** lines on the request/operation charts | There is no per-namespace rate limit to plot. `resource_exhausted_cause` tells you which internal limiter actually fired, which is the more useful answer. |
| "Failed Workflows" table linking to **cloud.temporal.io** | The deep link is Cloud-specific. Self-hosted, the route from a metric to an execution ID is the **Loki row** on the Golden Signals board — metrics structurally cannot carry `workflow_id` (unbounded cardinality), so the ID lives in the log line. |

---

## Panels that are empty on a healthy stack

Ten of 59 queries return nothing, all deliberately. Each is in the
`EXPECTED_EMPTY` allowlist in `demo/scripts/validate.sh` with its reason,
because **an undocumented empty panel is indistinguishable from a broken one.**

| Panel | Why empty | How to populate |
|---|---|---|
| Completions by outcome (failed/timeout/terminated) | SDK and server counters are **not created in Prometheus until they first increment** — absent, not zero | `make chaos-failures`, `make chaos-backlog` |
| Activity failures and retries | same | `make chaos-failures` |
| SignalWorkflowExecution latency | the demo app never signals | signal a Workflow |
| SignalWithStartWorkflowExecution latency | the demo app never signal-with-starts | use the pattern |
| Resource exhausted | no rate limiter has fired — the healthy state | `make chaos-backlog` |

---

## Using it on Temporal Cloud instead

If you *are* on Cloud, use the original — it is better there, and this repo's
`cloud/` bundle carries rules and dashboards built on the Cloud SLA. Two rules
that repo enforces and that catch most Cloud dashboard bugs:

- **`sum()`, never `rate()`,** on `temporal_cloud_v1_*` — they are already
  per-second gauges.
- **Percentiles arrive pre-computed** (`_p50`, `_p95`, `_p99`) and **cannot be
  re-aggregated.** Averaging a p95 across namespaces is not a p95.

Never load the `cloud/` and `production/` rule sets into one Prometheus —
`cloud/` is prefixed `cloudslo:*` precisely so it can coexist with
application-team rules, but `demo/` and `production/` both record `slo:*` and
would produce duplicate series and silently wrong SLO numbers.
