# Recommendations — every threshold, and why it is what it is

Each number in this repo is one of four things. **Knowing which one you are
looking at matters more than the number itself**, because it tells you whether
to keep it, tune it, or replace it before you go anywhere near production.

| | What it means | What to do with it |
|---|---|---|
| **Published** | Comes from Temporal's own documented guidance | Keep, unless you have measured a reason not to |
| **Measured** | Derived from running this stack and reading the result | Keep the *reasoning*; re-measure the *number* on your cluster |
| **Structural** | Exists to stop a rule misfiring, not to describe health | **Do not tune this away.** Removing it breaks the rule |
| **Placeholder** | An honest guess with no data behind it yet | **Replace before production** |

> **No threshold in this repo is yours until you have baselined it against two
> weeks of your own traffic.** Objectives especially. The SLIs are defensible;
> the objectives attached to them are placeholders, and `production/` ships some
> alerts with literal `REPLACE_ME` markers so they cannot be adopted by accident.

---

## 1. At a glance

| Threshold | Value | Kind | Where |
|---|---|---|---|
| Schedule-to-start P99 | `> 0.2` s | **Published** | `TemporalWorkflowTaskScheduleToStartHigh`, `TemporalActivityScheduleToStartHigh` |
| Poll success rate | `< 0.90` | **Published** | `TemporalMatchingStarved`, `TemporalWorkerFleetOverProvisioned` |
| Sync match rate | `< 0.95` | **Published** | `TemporalSyncMatchRateLow` |
| Frontend error ratio | `> 0.001` | **Measured** (aligned to the 99.9% SLO) | `TemporalFrontendErrorRateHigh` |
| Frontend latency P95, **per operation** | `> 1` s | **Placeholder** | `TemporalFrontendLatencyHigh` |
| Frontend latency SLI bucket | `le="0.5"` | **Placeholder** | `slo:sli_bad:*` |
| Workflow failure ratio | `> 0.10` | **Placeholder** | `TemporalWorkflowFailureRatioHigh` |
| Persistence errors | `> 0` | **Structural** | `TemporalPersistenceErrors` |
| Tasks with no poller | `> 0` | **Structural** | `TemporalTasksWithNoPoller` |
| Non-determinism | `> 0`, `for: 0m` | **Structural** | `TemporalNonDeterminismError` |
| Poll volume guard | `> 1` matched/s | **Structural** | all three matching rules |
| Starved second condition | s2s `> 0.2` s | **Measured** | `TemporalMatchingStarved` |
| Over-provisioned conditions | s2s `< 0.05`, sync `> 0.95` | **Measured** | `TemporalWorkerFleetOverProvisioned` |
| Burn rate fast / slow | `14.4` / `6` | **Published** (Google SRE) | `SLOFastBurn`, `SLOSlowBurn` |
| Workflow duration budget | `60` s, buckets `1,2,5` | **Placeholder** | `monitor/slo-config.demo.yaml` |
| Compliance window | `1h` lab / `28d` prod | **Structural** | `slo-rules.yml` |

---

## 2. Latency

### Schedule-to-start: `0.2` seconds — *published*

The single most important threshold here, and the only one to autoscale on.

Temporal publishes 200ms as the guidance for schedule-to-start, and it is
defensible on first principles: this is queue wait, not work. Any time here is
pure latency added before your code runs.

**Units are the trap, not the value.** This stack is the Go SDK, so the metric
is `temporal_activity_schedule_to_start_latency_seconds_bucket` and the
threshold is `0.2`. **TypeScript, Python and .NET emit milliseconds** under a
series name without the `_seconds` suffix, where the same threshold is `200`.
Copy the rule between SDKs and you have a silent 1000× error that makes the
alert either never fire or never stop. Run `make verify-sdk-labels`.

Measured on this stack for orientation: healthy baseline sits at **0.0088 s**;
`make chaos-slots` drives it to **10.0 s**.

### Frontend latency: `1` second P95, per operation — *placeholder*, and the exclusion is the real content

The `1s` is a guess. **The `operation!~"Poll.*"` exclusion next to it is not.**

Note it is **P95, not P99**, and grouped `by (operation)` — so it fires when *any
single* operation degrades, rather than letting a slow one hide inside a healthy
aggregate. That grouping is worth keeping even if you retune the second.

`PollWorkflowTaskQueue` and `PollActivityTaskQueue` block for up to **60 seconds
by design** and are the highest-volume Frontend operations. Measured here:

- Long-polls **included**: 95.9% of requests "under 500ms"
- Long-polls **excluded**: 100%

The shipped `TemporalFrontendLatencyHigh` rule fired **permanently on a healthy
idle cluster** before this exclusion was added. Tune the `1s`; do not remove the
exclusion.

The same mechanism collapses a *ratio* rather than a percentile — see §4.

### Frontend latency SLI: `le="0.5"` — *placeholder*

Bucket boundaries are not free-form: `histogram_quantile` can only report
boundaries the histogram actually has. Check your own buckets before picking a
number, or you will silently get the nearest one.

---

## 3. Error ratios

### Frontend errors: `0.001` — *measured, and deliberately aligned*

0.1% is not arbitrary — it is `1 - 0.999`, the frontend availability objective.
An alert threshold looser than its SLO lets you burn the entire budget without
paging; tighter, and it pages while you are still inside it.

**The `error_type` filter matters more than the number.** Temporal's Matching
service emits a steady **~0.39/s of `serviceerror_Canceled` at idle**. Counting
it took measured availability to **98.77%** and blew a 99.9% budget many times
over while nothing was wrong. Client faults — `Canceled`, `NotFound`,
`AlreadyExists`, `InvalidArgument`, `FailedPrecondition`, `QueryFailed` — are
not your service failing.

### Workflow failure ratio: `0.10` — *placeholder, and probably wrong for you*

10% of Workflows failing is a business threshold, not a platform one. A payments
Workflow at 10% failure is an incident; a best-effort enrichment Workflow at 10%
may be Tuesday.

**Watch what it does not catch.** Saturation on Temporal produces **timeouts,
not failures**. Measured under a backlog storm: `workflow_failed` sat at
**0.02/s** while `workflow_timeout` hit **24.6/s**. A failure-ratio alert
reported ~100% healthy while three quarters of all work expired. Pair it with a
timeout signal.

**The `or ... * 0` inside it is structural — do not simplify it away.** During a
total outage there are no successes, so the right-hand side of the ratio
produces no series at all, the whole expression returns empty, and the alert
stays **silent exactly when it matters most**. The `or <same vector> * 0` forces
a zero-valued series to exist so the division still evaluates. Every ratio alert
in this repo needs the same treatment.

It fires at `severity: critical` with `for: 2m` — short, because unlike fleet
sizing this is already user-visible by the time it trips.

### Persistence errors: `> 0` — *structural*

Not a rate, not a ratio. Any sustained datastore error is upstream of nearly
every other Temporal symptom — most self-hosted incidents are persistence
incidents wearing a different hat. Check this before scaling any service.

---

## 4. Matching and fleet sizing — the subtle ones

### Poll success rate: `< 0.90` **AND** schedule-to-start `> 0.2` — *the second condition is the point*

Poll success rate = matched / (matched + empty). Temporal's guidance is >90%
steady-state, >95% high-volume.

**This ratio cannot page anyone on its own**, and that is demonstrable rather
than theoretical. `make chaos-poller-flood` over-provisions the fleet and
produces, measured:

| | Baseline | Flooded |
|---|---|---|
| Poll success rate | 0.9995 | **0.6812** |
| Sync match rate | 0.6548 | **1.0000** (improved) |
| Schedule-to-start P99 | 0.4539 s | **0.0088 s** |
| Empty polls/sec | 0.0278 | **0.6552** |

Nothing is wrong. A long-poll blocks 60s by design, so a fleet with more pollers
than work spends its life returning empty. **A starved fleet and a flooded fleet
both push this ratio down and need opposite responses** — anyone alerting on it
alone adds Workers during a flood and makes it worse.

Hence:

- **`TemporalMatchingStarved`** — low poll success **AND** s2s `> 0.2`. Only
  starvation satisfies both. Verified silent for the entire flood.
- **`TemporalWorkerFleetOverProvisioned`** — low poll success, s2s `< 0.05`,
  sync match `> 0.95`. **`severity: info`, deliberately** (see §7).

The `0.05` upper bound on s2s is a "definitely not waiting" figure, comfortably
below the 0.2 action threshold so the two rules cannot both fire.

### Sync match rate: `< 0.95` — *published, and needs no second condition*

Sync match = delivered-to-a-waiting-Worker / all-delivered. Unlike poll success
rate, **it has no benign low state.** There is no healthy reason for Tasks to be
persisted before delivery. One condition plus a volume guard is enough.

Measured: healthy 1.0000; `chaos-backlog` drives it to **0.015** with the async
band at **625/s**.

### The volume guard: `> 1` matched poll/sec — *structural, do not remove*

On an idle queue every poll times out by design, both ratios collapse toward
zero, and an unguarded rule fires forever on a cluster that simply has nothing
to do. The guard is what makes the ratio meaningful.

Two related scoping decisions that are also structural:

- **`taskqueue!~"temporal_sys_.*"`.** Temporal's own system queues long-poll
  constantly and match almost nothing. Measured: `poll_timeouts` spans **68
  series across 6 namespaces** while `poll_success` spans **26 across 2**.
  Unscoped, poll success rate sits permanently low and barely moves during a
  real event.
- **Aggregate `by (namespace)`, not by queue.** Cluster metrics use `taskqueue`;
  the SDK histogram uses `task_queue`. An `and` joining on the queue label
  matches nothing and the alert is silently dead.

### Tasks with no poller: `> 0` — *structural*

The one signal in this repo with no false-positive mode. A Task queued where
nobody is listening is always wrong, usually a Task Queue name mismatch.

### Absence alerts — why `absent()` alone never works

`absent(temporal_worker_task_slots_available)` **never fires** on self-hosted.
Temporal's own internal Workers keep emitting it under
`namespace="temporal_system"` — **22 server-emitted series survive killing the
entire application fleet.** Scope absence alerts to your namespace *and* queue,
which is exactly why `production/` ships that rule commented out with
`REPLACE_ME` in it.

---

## 5. SLO objectives — all nine are placeholders

| SLI | Objective | Note |
|---|---|---|
| `frontend_availability` | 99.9% | |
| `frontend_latency` | 99.0% | |
| `history_availability` | 99.9% | |
| `history_latency` | 99.0% | |
| `matching_availability` | 99.9% | |
| `matching_latency` | 99.0% | |
| `persistence_availability` | 99.9% | |
| `workflow_completion` | 99.0% | business-owned, not platform-owned |
| **`worker_task_delivery`** | **90.0%** | deliberately the loosest — see below |

**The SLIs are defensible. The objectives are guesses.** An objective is a
negotiated promise about user pain, and nobody negotiated these.

`worker_task_delivery` at **90%** looks embarrassing next to 99.9% and is the
most honest number here. It measures the fraction of Activities starting within
200ms, and it is the SLI your *own fleet sizing* controls rather than Temporal.
Setting it at 99.9% means paging yourself every time a scale-up lags a traffic
spike. Measured: a 4-minute `make chaos-slots` run took it from **+44% budget
remaining to −13%** and put `SLOErrorBudgetExhausted` into pending — while every
cluster SLI stayed at 100%. The budget correctly blamed the Worker fleet, not
Temporal.

### Burn rate: `14.4` and `6` — *published*

Straight from Google's SRE workbook multiwindow/multi-burn-rate approach.
**14.4** = consuming a 30-day budget in ~2 days (page now). **6** = consuming it
in ~5 days (ticket). The dual-window construction (`1h` **and** `5m`) exists to
stop a brief spike paging you; both windows must agree.

### Compliance window: `1h` here, `28d` in production — *structural*

The lab uses 1 hour so a chaos scenario visibly moves the number inside a demo.
**A 1-hour window is not an SLO**, it is a demo aid. Production needs 28–30 days,
which also means raising Prometheus retention past 28d — see the note at the top
of `slo-rules.yml`.

---

## 6. Workflow duration SLO — the one nothing else can see

`monitor/slo-config.demo.yaml`: budget `60s`, buckets `[1, 2, 5]`, window `28d`,
objective `0.99`.

All **placeholders**, and the budget is per Workflow type by design — a 60s
budget is meaningless for a Workflow that waits on a human.

The buckets (1×, 2×, 5× budget) exist because *how far past* budget an execution
is changes the response: 1× is drifting, 5× is stuck. Measured during
`make chaos-stuck`: **0 → 5** executions at buckets 1 and 2 while **no other
alert fired anywhere on the stack**.

Two structural details worth keeping:

- **Terminating a stuck Workflow does not improve compliance.** The execution
  moves from "still running past budget" to "closed over budget" and the ratio
  is unchanged. Verified: compliance held at **0.793336** exactly across a
  termination.
- **The monitor never publishes a zero when a query fails.** A `0` would read as
  "nothing is over budget" during exactly the outage when that is least likely
  to be true. It leaves the previous value and exposes
  `temporal_slo_last_successful_poll_timestamp_seconds` so you can alert on
  staleness instead.

---

## 7. Thresholds that are deliberately *not* what you would expect

**`TemporalWorkerFleetOverProvisioned` is `severity: info`.** This is a cost
finding, not an incident: Workflows succeed, Tasks are picked up instantly, and
the only symptom is paying for idle pollers. Paging on it is precisely how a
team learns to scale *up* when it should scale *down*. Send it to a dashboard or
a cost review.

**`TemporalNonDeterminismError` is `for: 0m`.** One NDE is already one stuck
Workflow that will retry forever and never self-heal. There is no debounce worth
having. Its label is also `failure_reason`, **not** `error_type` — the published
rule matches nothing on the Go SDK via tally.

**`for: 15m` on the matching ratios, `30m` on the cost one.** Fleet sizing is not
an urgent question, and a shorter window catches every ordinary traffic trough.

**`for: 1m` on `TemporalWorkerFleetAbsent`.** The opposite reasoning: an absent
fleet is silent by construction, so the only defence is reacting fast.

---

## 8. What to change first, in order

1. **Replace every `REPLACE_ME`** in `production/prometheus/`. The absence alert
   is commented out on purpose — enabling it with the placeholder in place makes
   it fire constantly.
2. **Run `make verify-sdk-labels` against your own Workers.** It checks the two
   things that silently break every latency and NDE rule: unit (seconds vs
   milliseconds) and label name (`failure_reason` vs `error_type`).
3. **Baseline for two weeks before setting any objective.** Record the actual
   distribution, then set the objective at a level you were already meeting.
4. **Set `worker_task_delivery` from your own p99**, not from 200ms, if your
   Activities are naturally bursty.
5. **Set duration budgets per Workflow type** in `monitor/slo-config.yaml`. The
   60s default is only right for short synchronous work.
6. **Move the compliance window to 28d** and raise Prometheus retention to match.
7. **Add host CPU** (node_exporter or cAdvisor). It is not a Temporal metric, and
   without it `chaos-backlog` and `chaos-slots` are indistinguishable on the
   dashboard — half the runbook's decision branches are unusable.

---

## 9. How to tell a threshold is wrong

The failure mode that matters in this repo is not a noisy alert — those get
fixed. It is the alert that **looks correct in review and never fires**.

- **It has never fired.** Prove it can: every threshold here has a chaos scenario
  that moves it. If yours has none, you do not know it works.
- **It fires constantly on a healthy stack.** Usually a missing exclusion
  (long-polls), a missing filter (client faults), or a missing volume guard
  (idle queues) — not a number that needs raising.
- **It fires and you always take the same action regardless.** Then it is a
  dashboard panel, not an alert.
- **Its remedy depends on a second metric you are not looking at.** Poll success
  rate is the worked example: same value, opposite fixes.

`promtool check rules` and `terraform validate` prove syntax, not behaviour. A
rule that parses can still return empty forever. Query live data before
believing any of it.
