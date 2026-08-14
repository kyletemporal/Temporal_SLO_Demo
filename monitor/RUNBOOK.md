# Runbook — Workflow duration SLO and stuck executions

For the alerts in `demo/prometheus/monitor-rules.yml` and the **M — Workflow
duration SLO** row of the golden-signals board.

**Read this first:** the metrics in this row answer a question no other alert in
the stack can. Every Temporal workflow-outcome metric is a counter over
executions that **ended**. An execution that never ends increments none of them,
so a Workflow stuck forever is invisible to `workflow_failed`,
`workflow_timeout`, `no_poller_tasks` and every burn-rate alert built on them.
Reproduce it any time with `make chaos-stuck` in `demo/`.

That has a direct consequence for triage: **these alerts will fire while
everything else is green, and that is not a false positive.**

---

## `WorkflowsOverBudget`

> N executions running past 2× their duration budget.

### What it means

Executions are open and have exceeded twice the time you promised. They may be
progressing slowly, blocked on something external, or wedged permanently — this
alert does not distinguish those, and the first job is to find out which.

### The one thing metrics cannot tell you

**Which executions.** There is no `workflow_id` label here, deliberately — it is
unbounded, and Temporal omits it from SDK metrics for the same reason.

Get the IDs from Visibility:

```bash
temporal workflow list --query "WorkflowType = 'OrderWorkflow' \
  AND ExecutionStatus = 'Running' \
  AND StartTime < '2026-08-14T12:00:00Z'"
```

Set the timestamp to `now - (2 × budget)`. Read the budget off
`temporal_slo_budget_seconds{workflow_type="..."}` rather than from memory — it
is exported precisely so there is one source of truth.

Or use the **Find stuck executions** log panel on the golden-signals board,
which surfaces worker log lines carrying `WorkflowID` and `RunID` with derived-field
links straight into the Temporal UI.

### Triage

Work down this list; each step rules out a cause the one below it looks like.

1. **Is the whole fleet behind?** Check schedule-to-start and sync match in the
   RED rows above. If those are also bad, this is a capacity problem and these
   executions are victims, not the cause. Fix capacity; this clears on its own.

2. **Is anything failing?** Check `temporal_activity_execution_failed_total` and
   the Workflow Outcomes panel. A retry storm — an Activity failing forever under
   an unlimited retry policy — keeps a Workflow open indefinitely while
   `workflow_failed` stays flat. `make chaos-stuck` reproduces this exact shape.

3. **Non-determinism?** Check `TemporalNonDeterminismError` and grep worker logs
   for `TMPRL1100`. NDE executions retry the Workflow Task forever and will never
   self-heal. They need a code fix plus Worker Versioning.

4. **Blocked on something external?** If nothing above matches, open one
   execution in the Temporal UI and read its pending Activities. The common cases
   are an Activity awaiting a downstream service, or a Workflow blocked on a
   Signal that is never going to arrive.

5. **Is the budget wrong?** If these executions are behaving exactly as designed
   and the number is simply too low, the alert is correct and the *budget* is the
   bug. Budgets derived from a p99 describe what the system **has** done, not what
   users **need** — see `budget-derive`'s own warning. Fix it in config, not by
   silencing the alert.

### What NOT to do

**Do not terminate them to clear the alert.** It will not work, by design: the
SLI counts an over-budget open execution in the denominator already, so
terminating moves it from one denominator term to another and the compliance
number does not change. Verified — 5 executions moved from
`over_budget_executions{bucket="1"}` to `closed_over_budget` and compliance held
at `0.793336` exactly. The number cannot be improved by destroying the evidence.

Terminate when terminating is the right operational call, not to make a
dashboard look better.

---

## `MonitorPollsStale` — critical

> Visibility queries have not succeeded for over 5 minutes.

### What it means

**Every `temporal_slo_*` gauge is now showing its last known value, not current
state.** Treat the entire duration SLI as *unavailable*, not as good news.

This alert exists because of a deliberate design choice: when a Visibility query
fails, the monitor does **not** publish a zero. A zero would read as "nothing is
over budget" during exactly the outage when that is least likely to be true, and
it would look like healthy data on the board. Leaving the previous value in place
is safer — but a frozen gauge looks perfectly healthy too, and this alert is the
only thing that reveals it. Never disable this alert while keeping that
behaviour; the pair is what makes the design safe.

### Triage

1. `temporal_slo_poll_errors_total` — the `error_type` label names the cause.
   - `resource_exhausted` → the monitor is being rate limited. Raise `-pace`, or
     lengthen `poll_interval`. Note the monitor already backs off; sustained
     throttling means the namespace is genuinely at its limit.
   - `invalid_argument` → a query is malformed for this server version, most
     often a search attribute that does not exist here.
   - `unavailable` / `deadline_exceeded` → Temporal itself, or the network path.
     Check the service-health rows.
   - `permission_denied` → credentials. On Cloud, check the API key's role.
2. `docker compose logs slo-monitor` — every failed query is logged with its kind.
3. Confirm the Temporal Frontend is healthy before blaming the monitor.

---

## `StuckDetectionUnavailable` — info

> `TemporalReportedProblems` is unavailable in this namespace.

### What it means

Server-reported stuck detection is off, so `temporal_slo_stuck_executions` is
**not published at all**. Duration buckets still work and remain the primary
signal — this is a reduced capability, not an outage.

Absence rather than a zero is deliberate: a `stuck_executions` gauge sitting at 0
on a server that lacks the attribute is indistinguishable from a clean bill of
health.

### To enable it

Requires **Server 1.30+**. Self-hosted also needs dynamic config:

```yaml
system.numConsecutiveWorkflowTaskProblemsToTriggerSearchAttribute:
  - value: 3
```

On the version this lab defaults to (1.27.4) it is simply unavailable, and the
monitor probes for it at startup rather than assuming either way.

---

## Reading the row

| Panel | Healthy | What a bad value means |
|---|---|---|
| Duration compliance | ≥ objective | Executions are finishing late, or not at all |
| Open executions past budget | 0 | Live executions past budget **right now** |
| Running executions | scales with volume | Not alertable alone — context only |
| Poll freshness | < 120s | **The row is stale; do not trust it** |
| Server-reported stuck detection | 1 | 0 = reduced capability, not an incident |

Two panels there describe whether the **signal** works rather than whether the
**system** is healthy. That is not padding: this row's failure modes are silent
by construction, so the guards have to be as visible as the data.

---

## Escalation

- **Application team** (routed by `owner` in `slo-config.yaml`) — for executions
  stuck on their own logic: retry storms, non-determinism, waits on Signals that
  never arrive, budgets that were never realistic.
- **Platform team** — for `MonitorPollsStale`, Visibility rate limiting, or
  anything correlating with the service-health rows.
- **Temporal Support** — if Visibility is erroring with `unavailable` while the
  cluster otherwise looks healthy.
