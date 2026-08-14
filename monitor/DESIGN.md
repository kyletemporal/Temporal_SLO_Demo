# Workflow SLO & Stuck-Workflow Detection — Design

Review this before the code. Everything here is a decision I would rather have
corrected now than after two thousand lines.

---

## 1. Why a service exists at all

`workflow_schedule_to_close_latency` only emits **when a workflow closes**. A
permanently stuck execution never emits it, so latency percentiles look pristine
while executions rot in `Running`. No metric can see this, because the absence
of a metric *is* the symptom.

So: poll the Visibility API for counts, publish them as gauges, and compute the
SLI from those.

The second constraint shapes everything else: **duration is not the SLI,
progress is.** A workflow sleeping 30 days on a signal is healthy. Nothing here
treats age alone as failure — the age ladder is a *degradation* signal that only
becomes an SLO violation relative to a per-type budget a human agreed to.

---


## Framing correction: this is the SECOND-choice approach

Temporal's own guidance (Joshua Smith, *Temporal Cloud Observability*, July 2026)
ranks stuck-Workflow detection in this order:

1. **In-Workflow timers — recommended.** Put the deadline in the Workflow code
   and let the Workflow detect and handle its own overrun. Simplest and cleanest:
   no external service, no Visibility polling, no search-attribute dependency,
   and the Workflow can act on the timeout itself rather than just reporting it.
2. **External monitoring — what this service does.** Visibility queries plus
   custom search attributes, from the outside.

This service is therefore the complement, not the default. It earns its place for
Workflows you do NOT control — a platform team watching application teams' code
cannot add a timer to someone else's Workflow — and for fleet-wide SLI reporting
that no single Workflow can produce.

Say this to application teams before handing them a monitor: if they own the
Workflow code, a timer is the better answer, and this service should not be used
to paper over Workflows that were never given deadlines.


## 2. Blocking finding: `TemporalReportedProblems` availability

The definitive stuck query depends on a search attribute that is **not
universally available**. Verified against the demo cluster (Server 1.26.2):
`temporal operator search-attribute list` does not include it.

| | |
|---|---|
| Introduced | Server **1.30** |
| Self-hosted | **Off by default.** Needs `system.numConsecutiveWorkflowTaskProblemsToTriggerSearchAttribute` (default 5, `0` disables) |
| Cloud | On by default |
| Trigger | **N consecutive** Workflow Task failures (default 5); clears on first success |
| Completeness | **Not total.** A non-determinism error surfaced only via a Query does not set it, because queries can be delivered without a Workflow Task |

Three consequences baked into the design:

1. **Strategy is config-driven** (`deployment.stuck_detection: auto | reported_problems | fallback`). `auto` probes the cluster at boot.
2. **Capability is exported**, not assumed — `temporal_slo_stuck_detection_available`. Without it, the stuck gauge reads 0 forever on 1.26.x and *looks healthy*. Degraded mode must be loud.
3. **The fallback is not merely a downgrade path** — it is a supplement everywhere, because the SA misses the query-only case above.

---

## 3. Service boundaries

The monitor is **not a Worker.** It is a gRPC Visibility client plus an HTTP
server. No workflows, no activities, **no task queue**. It therefore cannot
share a worker fleet with what it monitors — the isolation requirement is
satisfied structurally rather than by configuration. Its only dependency is the
Frontend.

Two poll loops, deliberately different cadences:

| Loop | Default | Queries | Cost |
|---|---|---|---|
| Fast | `poll_interval` 60s | stuck, age ladder, running baseline | cheap, bounded |
| Window | `window_poll_interval` 600s | closed-in-budget / closed-over-budget across `slo_window` | expensive, 28d range |

Types are staggered across each interval with jitter. `RESOURCE_EXHAUSTED`
triggers exponential backoff. Floor of 30s enforced regardless of config —
Visibility is rate-limited, hardest on Cloud.

All counts come from **`CountWorkflowExecutions`**, never `ListWorkflowExecutions`.
We want counts, not pages. (The one exception is budget derivation — see §7.)

---

## 4. Metrics

| Metric | Type | Labels |
|---|---|---|
| `temporal_slo_stuck_executions` | gauge | `workflow_type, task_queue, cause` |
| `temporal_slo_over_budget_executions` | gauge | `workflow_type, task_queue, bucket` |
| `temporal_slo_running_executions` | gauge | `workflow_type, task_queue` |
| `temporal_slo_closed_in_budget` | gauge | `workflow_type, task_queue` |
| `temporal_slo_closed_over_budget` | gauge | `workflow_type, task_queue` |
| `temporal_slo_budget_seconds` | gauge | `workflow_type` |
| `temporal_slo_stuck_detection_available` | gauge | `namespace, method` |
| `temporal_slo_poll_duration_seconds` | histogram | `workflow_type, query_kind` |
| `temporal_slo_poll_errors_total` | counter | `workflow_type, query_kind, error_type` |
| `temporal_slo_last_successful_poll_timestamp` | gauge | `workflow_type, query_kind` |

**No `workflow_id`. No `run_id`.** Ever. Temporal omits `workflow_id` from SDK
metrics deliberately; reintroducing it here would blow up cardinality in exactly
the same way. `bucket` is bounded to the configured multipliers; `cause` is
bounded by Temporal's enum.

`temporal_slo_budget_seconds` is exported so recording rules read the budget
from the series rather than hardcoding it — one source of truth, and changing a
budget is a config edit that propagates without regenerating rules.

---

## 5. The SLI, and why the denominator is the whole point

```promql
slo:workflow_compliance:ratio28d =
      temporal_slo_closed_in_budget
    / ( temporal_slo_closed_in_budget
      + temporal_slo_closed_over_budget
      + sum without(bucket) (temporal_slo_over_budget_executions{bucket="1"}) )
```

**`sum without(bucket)` is not cosmetic.** `over_budget_executions` carries a
`bucket` label that the two closed gauges do not, and Prometheus binary
operators require identical label sets — so the obvious form of this expression
(which this document published until it was run against a live stack) matches
nothing and returns **empty**, not a wrong number. An SLI that silently
evaluates to no data is worse than one that is wrong, because no dashboard
panel and no burn-rate alert will tell you it is missing. Verified: without the
`sum without(bucket)` the query is empty; with it, 79.3336%.

`bucket="1"` is precisely "still running past 1× budget". An open workflow past
budget is **already** a violation and can never become compliant, so it belongs
in the denominator now, not when it eventually closes.

This is what makes terminating a stuck workflow *not* improve the number: the
execution moves from the third term to the second, and the ratio is unchanged.
That property falls out of the shape rather than being special-cased — but only
if terminal-but-late closures count as over-budget. That is explicit config, not
an implicit assumption:

```yaml
closed_over_budget_statuses: [Completed, Failed, Canceled, Terminated, TimedOut]
```

Computing compliance over closed executions alone would let the SLO *recover as
we destroy the evidence*. That is the single most important line in this
document.

### Stateless gauges, not counters

Window counts are point-in-time Visibility queries over `slo_window`, published
as gauges. The alternative — tracking closure deltas in the service — is
restart-fragile and double-counts on retry. Gauges cost heavier queries, which
is exactly why the window loop runs at 600s rather than 60s.

---

## 6. Config

```yaml
deployment:
  kind: self-hosted | cloud
  namespace: default
  address: temporal:7233
  stuck_detection: auto            # auto | reported_problems | fallback
defaults:
  poll_interval: 60s
  window_poll_interval: 600s
  buckets: [1, 2, 5]
  slo_window: 28d
  closed_over_budget_statuses: [Completed, Failed, Canceled, Terminated, TimedOut]
workflow_types:
  - name: OrderWorkflow
    task_queue: orders
    budget: 4h
    objective: 0.99
    owner: team-orders
    phase_attribute: null
```

Adding a workflow type to the SLO program is a config edit. Never a code change.

---

## 7. Budget derivation (step 1)

Percentiles of `ExecutionDuration` (nanoseconds, **closed executions only**) over
30 days, per type.

**Derived by binary search over `CountWorkflowExecutions`, not by paging
`ListWorkflowExecutions`.** To find p99, search for the duration `D` where
`count(closed AND ExecutionDuration <= D) / total ≈ 0.99`. That is ~20 count
queries per percentile instead of paging potentially millions of records, and it
uses the same API as the monitor. The cost is that results are approximate to a
configurable tolerance — stated in the output, and irrelevant when the proposal
is `3× p99` anyway.

Output: a summary table to stdout, and a starter `slo-config.yaml` with budgets
at **3× observed p99** and the raw percentiles retained as comments. Budgets are
written with a `# TODO: derived from N executions over 30d — review before
alerting` marker. Derived numbers do not become alert thresholds without a human.

---

## 8. Rules and dashboards land in the existing bundles

No fifth bundle. One generator, `tools/generate_visibility_rules.py`, emits:

| | Record prefix | Alert prefix |
|---|---|---|
| `production/prometheus/visibility-rules.yml` | `slo:` | `WorkflowSLO*` |
| `cloud/prometheus/visibility-rules.yml` | `cloudslo:` | `CloudWorkflowSLO*` |

Prefixes and alert names are distinct from the existing bundles' — a collision
between `production/` and `cloud/` alert names has already been fixed once in
this repo and will not be reintroduced.

**Stuck alerts are not burn-rate alerts.** `temporal_slo_stuck_executions > 0`
for 15m pages immediately. There is no acceptable steady-state rate of
non-determinism errors, poison payloads, or bad deploys.

Burn-rate alerts on the workflow compliance ratio follow the existing
multiwindow ladder (14.4×/6× page, 3×/1× ticket) and route by `owner`.

---

## 9. Correctness constraints carried from the platform layer

Both already enforced elsewhere in this repo; both re-verified for the new rules:

1. **No `rate()`, `increase()`, `irate()` or `histogram_quantile()` on
   `temporal_cloud_v1_*`.** They are pre-computed per-second rates with delta
   temporality — `sum()` only. `rate()` remains correct for `temporal_*` SDK
   metrics and self-hosted server metrics.
2. **SDK histogram units differ.** Go and Java emit **seconds**; TypeScript,
   Python and .NET emit **milliseconds**. 200ms is `0.2` in Go/Java and `200`
   elsewhere. Thresholds are generated from `sdk_languages` and every one
   carries a unit comment. Getting this wrong is a silent 1000× error.

Also carried forward: `temporal_cloud_v1_approximate_backlog_count` resets to
zero on idle queues, so it is never used alone as evidence of no backlog.

---

## 10. Anti-patterns this design refuses

- **No Workflow Execution Timeouts as SLA enforcement.** They convert a fixable,
  inspectable problem into a dead workflow with destroyed state. In-workflow SLA
  breach notification belongs in a detached Timer inside the workflow.
- **No alerting on absolute count of running workflows.** It scales with
  business volume, not health. That is why `temporal_slo_running_executions`
  exists as a denominator and a dashboard line, and appears in no alert.
- **No `workflow_id` / `run_id` labels.**
- **No co-location with the monitored fleet.**
- **No `temporal_cloud_v0_*`.** Deprecated, sunset 2026-10-05, and v0→v1 names
  differ by more than the prefix.

---

## 11. Validation plan, and its one honest gap

| Acceptance criterion | How |
|---|---|
| Non-determinism detected < 15m, pages owner | **Fallback path only** on 1.26.2 |
| 5× over budget lands in bucket, ratio drops **while running** | Long-running workflow against a short budget |
| Terminating does not improve historical compliance | Terminate, re-read the ratio |
| Monitor down alerts within 3 poll intervals | Kill the container |
| No `rate()` on `temporal_cloud_v1_*` | Automated grep in the sweep |
| Every latency threshold carries a unit comment | Automated grep |

**The gap:** criterion 1 cannot be proven against `TemporalReportedProblems` on
1.26.2. I can validate the fallback end to end and the SA path only by
construction. If you want it genuinely proven, add a 1.30+ dev server to the
compose file for testing — it does not need to touch the 1.26.2 demo.

Second gap: the demo has **one** workflow type, so multi-type staggering and
jitter get unit tests rather than a real load test.

---

## 12. Build order

1. **Budget derivation** ← this step
2. Monitor service
3. Recording rules
4. Alerts
5. Dashboard rows
6. Runbook

Each independently reviewable; each works before the next begins.
