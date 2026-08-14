# Workflow SLO & Stuck-Workflow Detection

Read [`DESIGN.md`](DESIGN.md) first. It carries the decisions, the blocking
finding about `TemporalReportedProblems`, and the validation plan.

## Status

| Step | State |
|---|---|
| 1. Budget derivation | **done** — runs against a live cluster |
| 2. Visibility monitor service | **done** — runs in the demo stack, proven against `chaos-stuck` |
| 3. Recording rules | **done** — `demo/prometheus/monitor-rules.yml` |
| 4. Alerts | **done** — over-budget, poll staleness, detection availability |
| 5. Dashboard rows | not started |
| 6. Runbook | not started |

## Step 1 — derive starter budgets

```bash
go run ./cmd/budget-derive \
  -address temporal:7233 -namespace default \
  -lookback 720h -tolerance 100ms -max-duration 5m \
  -out slo-config.generated.yaml
```

Percentiles of `ExecutionDuration` are derived by **binary search over
`CountWorkflowExecutions`**, not by paging `ListWorkflowExecutions`. Cost is
logarithmic in the search range and independent of how many executions exist —
verified against 8,868 closed executions in 49 count queries, and unit-tested to
confirm 1k and 100k datasets cost identically.

Output is a starter `slo-config.yaml` with budgets at 3× observed p99, every one
marked `TODO`, with the raw percentiles retained as comments. **Derived numbers
must not become alert thresholds without a human agreeing to them.**

## Tests

```bash
go test ./...
```

The heaviest coverage is on query construction, because the timestamp arithmetic
in the age ladder is the easiest place here to be silently wrong: an off-by-one
on a bucket boundary crashes nothing and quietly moves executions between SLO
buckets.

## Step 2 — run the monitor

It ships in the demo stack:

```bash
cd ../demo && ./deploy.sh
curl -s localhost:9111/metrics | grep temporal_slo_
```

Config is `slo-config.demo.yaml`, which is **reviewed**. The generated
`slo-config.generated.yaml` is deliberately *rejected* at load time while it
still contains `owner: TODO-team` and TODO budgets — the service must not start
on numbers nobody has agreed to.

### Prove it does something Prometheus cannot

```bash
cd ../demo
make chaos-stuck            # parks Workflows that never end
# wait ~2 minutes (demo budget is 60s)
curl -s localhost:9111/metrics | grep over_budget_executions
make chaos-stuck-release    # REQUIRED cleanup
```

Measured on this stack: `over_budget_executions` went **0 → 5** at buckets 1 and
2 while **no Prometheus alert fired at all**, because every Temporal
workflow-outcome metric is a counter over executions that ENDED.

### Two properties worth knowing

**Terminating a stuck Workflow does not improve the SLI.** Verified end to end:
on release, 5 executions moved from `over_budget_executions{bucket="1"}` to
`closed_over_budget` (4374 → 4379) and compliance held at `0.793336` exactly.
An open execution past budget is already a violation, so it sits in the
denominator from the moment it crosses.

**A failed poll never publishes a zero.** Gauges keep their last value, because
a 0 would read as "nothing is over budget" during precisely the outage when that
is least likely to be true. The cost is that a frozen gauge looks healthy, which
is why `MonitorPollsStale` alerts on
`last_successful_poll_timestamp_seconds`. Do not remove one without the other.

### On servers below 1.30

`TemporalReportedProblems` needs Server 1.30+ and, self-hosted, a dynamic-config
setting. The monitor **probes** for it at startup rather than assuming. When it
is absent — including the 1.27.4 this lab defaults to — `stuck_executions` is
**not published at all**, and `stuck_detection_available{method="reported_problems"}`
reports 0. Absence is deliberate: a `stuck_executions` gauge sitting at 0 would
be indistinguishable from a clean bill of health. Duration buckets work on every
version and remain the primary signal.
