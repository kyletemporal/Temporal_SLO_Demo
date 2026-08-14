# Workflow SLO & Stuck-Workflow Detection

Read [`DESIGN.md`](DESIGN.md) first. It carries the decisions, the blocking
finding about `TemporalReportedProblems`, and the validation plan.

## Status

| Step | State |
|---|---|
| 1. Budget derivation | **done** — runs against a live cluster |
| 2. Visibility monitor service | not started |
| 3. Recording rules | not started |
| 4. Alerts | not started |
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
