# FR: `task_accepted_latency` — the unmeasured segment of the task path

**Status:** filed upstream · **Owner:** TBD · **Raised:** 2026-09-03
**Upstream:** [temporalio/temporal#11916](https://github.com/temporalio/temporal/issues/11916)

## The gap

There is no metric covering the interval from a task-producing RPC being
received to the resulting history task being durably committed and eligible for
dispatch.

`RespondWorkflowTaskCompleted` carries a bundle of Workflow Tasks and Activity
Tasks that History must persist before Matching can dispatch them. Every
*segment* of that path is instrumented. The transition **from the RPC into the
task queue** is not.

This started as a question from Kevin Woo — whether the absence of a
Worker→Server task acceptance metric was a real visibility gap or just something
he could not find. It is real, but narrower and more specific than "no metric
exists", which is why it was worth measuring before filing.

## What already exists

Quoted from `common/metrics/metric_defs.go` on `main`, not from memory:

| Metric | Description in the source |
|---|---|
| `task_latency_queue` | "End-to-end latency for processing and completing a history task, **from task generation** to completion." |
| `task_latency_load` | "Latency from history task **generation to loading into memory**." |
| `task_latency_schedule` | "Latency from history task loading to start processing." |
| `task_latency_processing` | "Latency for processing a history task one time." |
| `task_latency` | "…across all attempts but excludes any latencies related to workflow lock or user quota limit." |

**The load-bearing word is "generation".** Every one of these begins after the
task already exists. None covers request received → task generated and
committed.

## Measured

Server 1.27.4, `temporalio/auto-setup`, single Worker under steady load. p99,
3-minute windows:

| Segment | Metric | p99 |
|---|---|---|
| **1** — Worker → Frontend → History persisted | `service_latency{operation="RespondWorkflowTaskCompleted"}` | 9.89 ms |
| | `persistence_latency{operation="UpdateWorkflowExecution"}` | 4.96 ms |
| **2** — task generated → dispatched | `task_latency_queue{operation="TransferActiveTaskActivity"}` | 85.85 ms |
| | ├ `task_latency_load` | 49.69 ms |
| | ├ `task_latency_schedule` | 0.99 ms |
| | └ `task_latency_processing` | 9.23 ms |
| **3** — Matching → poller | `task_dispatch_latency{task_type="Activity"}` | 447.31 ms |
| | SDK `temporal_activity_schedule_to_start_latency_seconds` | 460.10 ms |

Laptop-scale figures from a deliberately small deployment — included for the
**shape**, not as representative production latencies. The close agreement
between `task_dispatch_latency` (447 ms) and SDK schedule-to-start (460 ms) is a
useful check that segment 3 is measured correctly.

**These cannot be summed.** Three services, three histograms, no shared
exemplar. Adding p99s does not produce a p99, so end-to-end acceptance latency
is not derivable from what exists.

## Why it matters here

This repo's `worker_task_delivery` SLI is built on SDK schedule-to-start. When
that SLI burns budget, the runbook's decision tree splits on host CPU to
separate *too few Workers* from *concurrency limit too low* — see
[`RECOMMENDATIONS.md`](../RECOMMENDATIONS.md) §4.

Both branches assume the delay is on the **dispatch** side. Neither can rule out
the accept-and-commit step, because nothing measures it. In practice that is a
third branch the runbook cannot currently offer, and unlike the other two it has
no proxy to infer it from.

Note `task_latency_load` is already the largest contributor above — 49.69 ms of
85.85 ms, 58% — and it is the segment that degrades under shard pressure. Any
new metric should sit alongside it rather than obscure it.

## Cloud is worse than self-hosted

All 57 documented `temporal_cloud_v1_*` metrics were enumerated from the
[OpenMetrics metrics reference](https://docs.temporal.io/cloud/metrics/openmetrics/metrics-reference).
**None of the `task_latency_*` family is exposed**, nor `task_dispatch_latency`,
nor `task_schedule_to_start_latency`.

The closest Cloud metrics are `temporal_cloud_v1_service_latency_p99` (frontend
RPC) and `temporal_cloud_v1_approximate_backlog_count` (queue depth) — one on
either side of the entire history-task path, with nothing between.

So for a Cloud customer the gap is not just the accept interval: *every* segment
between the RPC and the poller is unavailable. A Cloud customer seeing elevated
schedule-to-start has no server-side metric to attribute it with. This is
consistent with what [`CLOUD-TO-SELFHOSTED.md`](CLOUD-TO-SELFHOSTED.md) already
documents — the Cloud metric set is deliberately narrower — but it is the first
case found where the narrowing removes the only available diagnosis.

This may be the higher-value half of the request.

## What was asked for upstream

One histogram on History — `task_accepted_latency`, RPC receipt → task committed,
labelled `namespace` and `task_category` so it composes with the existing family.

Deliberately **one metric, not a new family**: the sub-segments are covered,
only the leading interval is missing.

Explicitly scoped *out*: end-to-end stitching across Frontend → History →
Matching. That is a real gap too, but another histogram will not fix it — the
honest answer is trace context propagated through the task, which is a larger
change and was raised as an alternative rather than folded in.

## Reproducing it

Any self-hosted cluster with `PROMETHEUS_ENDPOINT` enabled, under load:

```promql
# Segment 1 — the RPC
histogram_quantile(0.99, sum by (le) (rate(
  service_latency_bucket{operation="RespondWorkflowTaskCompleted"}[3m])))

# Segment 2 — starts at task GENERATION, not at RPC receipt
histogram_quantile(0.99, sum by (le) (rate(
  task_latency_queue_bucket{operation="TransferActiveTaskActivity"}[3m])))

# Segment 3 — Matching to poller
histogram_quantile(0.99, sum by (le) (rate(
  task_dispatch_latency_bucket{task_type="Activity"}[3m])))
```

The gap is between the first and second queries.

One measurement note, since it cost time: these counters exist on an idle
cluster but `rate()` over them returns nothing without live traffic, and a
`rate()` window that has aged out reads identically to "the metric is missing".
Drive load and sample *during* it.

## Open questions

1. Is the accept-to-commit interval considered adequately covered by
   `persistence_latency{operation="UpdateWorkflowExecution"}`? If so the gap is
   the unattributed remainder between that and `service_latency`.
2. Is there an intentional reason `task_latency_*` is not exposed on Cloud —
   cardinality, or a deliberate abstraction boundary?
3. Would trace propagation be preferred over a new histogram? It solves the
   stitching problem too, at higher cost.

## If it lands

Add a fourth branch to the schedule-to-start decision tree in
[`RECOMMENDATIONS.md`](../RECOMMENDATIONS.md) §4, and a panel to the golden
signals board's saturation row. Neither is worth doing speculatively.
