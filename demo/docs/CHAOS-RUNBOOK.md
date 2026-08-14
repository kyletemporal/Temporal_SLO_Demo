# Chaos Runbook — Driving the Dashboard

Each scenario is engineered to move a specific set of panels in a specific
direction. The value is not that metrics move; it is that different root causes
produce *distinguishable* shapes, and that a couple of them are
*indistinguishable* without data the dashboard does not carry.

Run them in order. Scenario 0 is not optional.

---

## Scenario matrix

| # | Scenario | Command | Root cause | Panels that move |
|---|---|---|---|---|
| 0 | Baseline | `make baseline` | none | all green — this is your reference |
| 1 | Backlog storm | `make chaos-backlog` | not enough Worker capacity | schedule-to-start ↑, sync match ↓, slots → 0 |
| 2 | Failure injection | `make chaos-failures` | flaky dependency, then bad error handling | Workflow Outcomes: green then red |
| 3 | Orphan queue | `make chaos-orphan` | Task Queue name mismatch | no-poller tasks ↑ **only** |
| 4 | Slot saturation | `make chaos-slots` | concurrency limit set too low | slots → 0, schedule-to-start ↑, load stays LOW |
| 5 | Worker blackout | `make chaos-blackout` | Worker fleet down | SDK panels go **blank**, cluster panels go red |
| 6 | Stuck Workflows | `make chaos-stuck` | parked forever / infinite retry | **parked: NOTHING moves.** retry-storm: activity failures ↑ only |
| 7 | Non-determinism | `make chaos-nde` | Workflow code changed under open executions | NDE alert fires, `TMPRL1100` in logs with WorkflowID |

---

## The four lessons worth landing

### 1. Two panels, one signal, two different fixes

Scenarios 1 and 4 produce **nearly the same dashboard shape**: schedule-to-start
latency climbing, task slots at zero. The correct responses are opposite.

| | Scenario 1 | Scenario 4 |
|---|---|---|
| Schedule-to-start P99 | high | high |
| Task slots available | 0 | 0 |
| Frontend request rate | **high** | **low** |
| Worker host CPU | **high** | **low** |
| Right fix | add Workers | raise `MAX_CONCURRENT_ACTIVITIES` |
| Wrong fix | — | adding Workers (costs money, fixes nothing) |

The discriminator that actually settles it — **Worker host CPU** — is not a
Temporal metric. It comes from node_exporter or cAdvisor. Run `docker stats`
alongside scenario 4 to see it.

This is the concrete argument for the "get node-level metrics alongside this
dashboard" recommendation. Without them, two of the runbook's decision branches
collapse into a coin flip.

To prove the point, fix scenario 4 without adding a single Worker:

```bash
MAX_CONCURRENT_ACTIVITIES=200 docker compose up -d worker
```

Same load, same hardware, problem gone.

### 2. Activity failures are not Workflow failures

Scenario 2 runs the **same** Activity failure rate in both phases. Only the
retry policy changes.

- **Phase A** (`maxAttempts: 3`) — Activity failures high, Workflows succeed.
  A healthy application absorbing a flaky dependency. **Do not page on this.**
- **Phase B** (`maxAttempts: 1`) — identical Activity failures, Workflows fail.
  **Page on this.**

Alerting on raw Activity failure count guarantees false pages. The ratio is the
signal:

```promql
sum(rate(workflow_failed[5m])) / clamp_min(sum(rate(workflow_success[5m])), 1)
```

Temporal's own guidance is that Workflows should be designed to always succeed —
a Workflow that fails because an Activity ran out of retries usually means the
Workflow was never taught how to handle that failure.

### 3. Absent Workers emit no metrics

Scenario 5 is the one that changes how people build their alerts.

When the Worker fleet dies, the Worker Fleet Health panels do not go red. They
go **blank**. There is no data, because the thing that produces the data is
gone. A threshold alert on `temporal_worker_task_slots_available < 1` will never
fire, because the series does not exist to be evaluated.

What catches it is the cluster side — `no_poller_tasks` climbing and sync match
rate collapsing — because the Temporal Service is still running and still
reporting.

Practical consequences:

- Never rely on SDK metrics alone. Cluster metrics are the backstop.
- Add an absence alert. Something like:
  ```promql
  absent(temporal_worker_task_slots_available)
  ```
- Test alerts by killing the thing they watch, not by crossing a threshold.

Scenario 5 also demonstrates the durability guarantee: nothing fails during the
blackout. Tasks queue and wait, then drain when Workers return. That recovery
curve is usually the most persuasive thirty seconds of the whole demo.

---

### 4. Some failures move no metric at all

`make chaos-stuck` starts Workflows that park on a Signal that never arrives.
They are `Running`, pollers are healthy, no errors, nothing retrying, slots free.

Measured over 25+ minutes: `workflow_success`, `workflow_failed`,
`workflow_timeout` and `no_poller_tasks` all stayed at **0.0000**. Every
dashboard stays green while the business outcome never happens. This is the
"I thought something was going to happen but it didn't" page.

The reason is structural, not a gap in the dashboards: **every Prometheus counter
here describes a Workflow that ENDED.** One that never ends increments nothing.
Only its DURATION is wrong, and no metric carries duration for an open execution.
That is the entire argument for querying Visibility — see [`monitor/`](../../monitor/).

Two things to say out loud when demoing this:

- **An execution timeout converts this into a visible failure.** The lab caps
  normal Workflows at 10m, and with that cap these ended `TimedOut` and burned
  error budget within minutes. `chaos-stuck` deliberately runs with **no**
  execution timeout, which is Temporal's default, to show the real exposure.
  Setting one is a genuine mitigation.
- **The retry-storm variant is the near-miss.** Activity failures climb, so it
  looks detected — but `workflow_failed` stays flat, so the failure-ratio alert
  never fires while Actions burn indefinitely.

Cleanup is **required**: `make chaos-stuck-release`. With no execution timeout
these run until the stack is torn down.

---

## Suggested 30-minute demo flow

| Time | Action | Talking point |
|---|---|---|
| 0:00 | `make up`, then `make verify` | Setup is not done until data is queryable at the destination |
| 0:05 | `make baseline`, leave running | You cannot tune an alert you have never baselined |
| 0:12 | `make chaos-orphan` | The one panel with no false-positive mode |
| 0:17 | `make chaos-backlog`, then `make scale-up` mid-run | Signal → action → recovery, closed loop |
| 0:24 | `make chaos-slots` + `docker stats` | Why "add more Workers" is often wrong |
| 0:28 | `make chaos-blackout` | Absent Workers emit no metrics |
| 0:32 | `make chaos-stuck` (then release) | Some failures move **no** metric at all — why Visibility polling exists |

Scenario 2 is worth its own session if the audience owns application code
rather than infrastructure.

---

## Reading the recovery, not just the failure

The most common mistake when demoing this is to trigger the failure, point at
the red panel, and move on. The recovery is where the learning is:

- **Scenario 1** — after `make scale-up`, schedule-to-start should fall back
  toward zero within a minute or two. If it does not, the constraint was never
  Worker count.
- **Scenario 3** — recovery requires a *code or config* change, not a scaling
  action. Nothing you do to the Worker fleet drains that queue.
- **Scenario 5** — the drain rate on recovery tells you the fleet's real
  throughput ceiling, which is a number worth writing down.

---

## Resetting between scenarios

```bash
make scale-reset          # back to 1 worker
docker compose restart worker   # clears in-flight state
```

Wait for schedule-to-start to return to baseline before starting the next
scenario, or you will be reading the tail of the previous one.

For a completely clean slate (drops all Temporal history and metrics):

```bash
make down && make up
```
