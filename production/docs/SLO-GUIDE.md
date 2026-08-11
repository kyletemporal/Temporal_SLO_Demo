# SLO Guide — Error Budgets for a Self-Hosted Temporal Service

Nine SLIs covering every Temporal service role, their objectives, the error
budget maths, and burn-rate alerting.

**Boards:** *Temporal — Golden Signals (RED + Saturation)* and *Temporal SLO Board*
**Rules:** `prometheus/slo-rules.yml`

Everything here was measured against a running stack, not derived from
documentation. Where a number looks odd, there is a measurement behind it and
it is called out.

---

## 1. What is actually being promised

| SLI | Tier | Scope | Good event | Objective |
|---|---|---|---|---|
| `frontend_availability` | tenant | per namespace | gRPC request that did not fault | 99.9% |
| `frontend_latency` | tenant | per namespace | request served < 500ms | 99% |
| `workflow_completion` | tenant | per namespace | Workflow succeeded (timeout counts as bad) | 99% |
| `task_delivery` | tenant | per namespace | Activity Task started < 200ms | 99% |
| `history_availability` | infra | cluster | request that did not fault | 99.9% |
| `matching_availability` | infra | cluster | request that did not fault | 99.9% |
| `persistence_availability` | infra | per service role | datastore op that did not fault | 99.9% |

**Tenant SLIs are per-namespace.** That is your isolation boundary and how you
answer "is it us or is it you" without guesswork. **Infra SLIs are shared** —
they describe your platform's health, which is your problem, not any one
customer's. Internal namespaces (`temporal_system`, `system`, `_unknown_`) are
excluded throughout; including them measures Temporal's own housekeeping as if
it were customer traffic.

`persistence_availability` keeps its `service_name` label, so one definition
produces one SLO per role — **including `worker` and `server`**, which serve no
gRPC traffic of their own and would otherwise have no SLO at all. That is how
"all Temporal services" ends up covered by nine definitions rather than
fifteen.

### The objectives are placeholders. The SLIs are not.

99.9% and 99% are round numbers chosen to be achievable at this bundle's baseline.
A real objective is negotiated from what users need and what the system has
historically delivered — run a representative load test for a week before promoting any of
these. The *SLI definitions*, by contrast, were corrected against measurement
and are the part worth copying.

---

## 2. Three corrections that changed the numbers

Every one of these was found by building the SLI, looking at the result, and
disbelieving it. They are the reason a naively-written Temporal SLO reads as a
permanent outage.

### 2.1 Long-polls are not slow requests

`PollWorkflowTaskQueue` and `PollActivityTaskQueue` block for up to **60
seconds** by design — that is how a Worker waits for work. They are also the two
highest-volume operations on the Frontend:

```
PollWorkflowTaskQueue          7.158/s
RespondWorkflowTaskCompleted   6.677/s
PollActivityTaskQueue          5.622/s
StartWorkflowExecution         1.669/s
```

Include them in a latency SLI and you are measuring how long Workers sit idle.
Measured on this stack: **95.9%** of Frontend requests under 500ms with polls
included, **100%** with them excluded. The SLI reported a fake 4% failure rate
that no user experienced.

**`Poll.*` alone is not enough.** Two matching operations are long-poll watches
whose names do not begin with "Poll": `GetTaskQueueUserData` and
`ListNexusEndpoints`. They are invisible under load and glaring at idle — with
only `Poll.*` excluded, `GetTaskQueueUserData` is 96% of matching's remaining
traffic on an idle cluster and drove `matching_latency` to 4.8% attainment, a
**9,400% error budget overspend on a system doing nothing wrong**.

Every latency SLI here carries:

```
operation!~"Poll.*|GetTaskQueueUserData|ListNexusEndpoints"
```

Operation names change between server versions, so **re-derive this list on your
own cluster** instead of trusting it. Long-polls identify themselves by
behaviour — run this and exclude anything near the top:

```promql
(
  sum by (service_name, operation) (rate(service_latency_bucket{le="+Inf"}[2h]))
  -
  sum by (service_name, operation) (rate(service_latency_bucket{le="0.5"}[2h]))
)
/
sum by (service_name, operation) (rate(service_latency_bucket{le="+Inf"}[2h]))
```

On this stack it produces a clean split — six operations between 92% and 100%,
everything else under 1%:

```
matching   ListNexusEndpoints      100.00%
frontend   PollActivityTaskQueue   100.00%
frontend   PollWorkflowTaskQueue   100.00%
matching   PollWorkflowTaskQueue   100.00%
matching   GetTaskQueueUserData     95.32%
matching   PollActivityTaskQueue    92.76%
```

The same contamination affected the shipped **alert** rules, not just the SLIs:
`TemporalFrontendLatencyHigh` computes P95 *per operation* with no filter, so a
long-poll's P95 sits permanently above any threshold and the alert fired
forever on a healthy idle cluster. It now carries the same exclusion.

### 2.2 Client errors are not service failures

Matching emits a steady stream of `serviceerror_Canceled` on `AddWorkflowTask`
and `AddActivityTask` — routine context cancellation, roughly 0.39/s at idle on
this stack. Counting it as a fault took `matching_availability` to **98.77%**,
which burned an entire 99.9% error budget eleven times over while nothing was
wrong. With client-caused errors excluded it reads **100%**.

Excluded: `Canceled`, `NotFound`, `NamespaceNotFound`, `AlreadyExists`,
`InvalidArgument`, `FailedPrecondition`, `WorkflowExecutionAlreadyStarted`,
`QueryFailed`.

Kept, because they are yours: `Unavailable`, `Internal`, `ResourceExhausted`,
`DataLoss`.

This list is a starting point, not gospel — if your callers legitimately depend
on `NotFound` succeeding, move it.

### 2.3 An SLO you cannot meet is not an SLO

`task_delivery` measures Activity Tasks starting within 200ms. Whether that is
achievable is a property of your Worker configuration, not of Temporal.

Temporal's default `MaxConcurrentActivityExecutionSize` is 1000. If yours is
lower — and people lower it — schedule-to-start climbs under perfectly normal
load and this SLI is breached for reasons that are not faults. Measured on a
fleet pinned to 10 concurrent Activities:

| Activity Tasks started within | Share |
|---|---|
| 50ms | 69.6% |
| 100ms | 82.2% |
| **200ms** | **94.4%** |
| 500ms | 99.9% |

The fleet was not broken; it was small. Before promising 99% here, check what
your configuration can actually deliver, then either raise the concurrency or
lower the promise. Setting an objective you miss at baseline puts the board
permanently red and teaches everyone to stop looking at it.

Serverless Workers change this completely — see `SERVERLESS-WORKERS.md`.

## 3. How the maths works

Everything is recorded as a **bad-event ratio** (bad ÷ total), because error
budgets are defined on bad events. Availability is `1 - bad_ratio`.

```
slo:sli_bad:ratio_rate<w>         per-SLI bad ratio at 5m / 30m / 1h / 6h
slo:objective:ratio               the promise, e.g. 0.999
slo:error_budget:ratio            1 - objective  (the bad events you may spend)
slo:burn_rate:ratio_rate<w>       bad ratio ÷ error budget
slo:error_budget_remaining:ratio  1 - (bad over window ÷ error budget)
```

Layer 2 is written **once** and applies to every SLI by matching on the `sli`
label. Adding a tenth SLI means adding layer-1 rules only; budgets, burn rates,
and alerts pick it up automatically.

**Burn rate** is the number that matters. A burn rate of 1.0 means you are on
pace to spend exactly 100% of the budget by the end of the window. 14.4 means
you will spend it in 1/14.4 of the window — about two hours of a 30-day budget.

**Budget remaining goes negative**, deliberately. `-0.5` means you spent 150% of
what you were allowed. Clamping it at zero throws away the only number that
distinguishes "just missed" from "catastrophically missed".

**On the board it saturates at -100%, though.** A severe incident can burn many
multiples of the budget — measured under overload, `workflow_completion` read
**-7485%** against a short window. That is unreadable, and past -100% it has
stopped answering a question anyone acts on — the budget is gone either way.
The board clamps the *display* at -100% and leaves magnitude to the two columns
that express it properly: **Attained** (27.4% of Workflows completed) and **burn
rate** (72.6x). The underlying recording rule is unclamped, so alerts and
queries still see the true value.

### What saturation actually does to the budget

Measured under a deliberate overload. The numbers are not what most people predict:

```
workflow_success  8.45/s
workflow_failed   0.02/s      <- almost nothing FAILED
workflow_timeout 24.60/s      <- three quarters TIMED OUT
```

The storm does not produce errors. It produces **timeouts**: work queues behind
a 10-slot Worker until it passes `WorkflowExecutionTimeout` and expires. An SLI
that counted only `workflow_failed` would have reported ~100% attainment while
three quarters of all work silently never completed. That is the entire reason
timeouts are counted as bad events here.

### Two PromQL traps this file works around

**Missing series are not zero.** If an error counter has never incremented, its
series does not exist, and `missing / total` returns *nothing* — the SLI
silently vanishes rather than reporting a healthy 0%. Worse, in the inverse case
— every request failing, no successes — the naive form *also* returns nothing,
so a total outage produces no signal. Hence the `or <total> * 0` guard on every
ratio: it manufactures a zero-valued series with matching labels.

**The first burst of failures is invisible.** A counter that springs into
existence already at 40 produces `rate() = 0`, because Prometheus treats the
first sample it ever sees as the baseline — there is no earlier value to
subtract. Measured here: 40 Workflows failed, `workflow_failed` read 40, and
`rate(workflow_failed[5m])` was **0**. A second burst produced 0.2414/s and the
alert went pending immediately.

So on a system that has never failed before, the very first incident is muted
for one scrape-to-increment cycle. This is inherent to counters, not a bug to
fix, and it is a good reason not to rely on a single failure-rate alert as your
only safety net — `absent()`-style and cluster-side signals do not share the
behaviour.

**Label shapes must match to join.** `slo:objective:ratio` carries only `sli`,
but `persistence_availability` series also carry `service_name`. A table joins
columns by identical labels, so mixing the two shapes splits every persistence
row in half with nulls on both sides. `slo:objective_expanded:ratio` multiplies
the SLI by zero and adds the objective, copying the value onto the wider label
set.

---

## 4. Burn-rate alerting

Three alerts, in `slo-rules.yml`:

| Alert | Condition | Severity |
|---|---|---|
| `SLOFastBurn` | burn > 14.4 over **1h** AND over **5m** | critical |
| `SLOSlowBurn` | burn > 6 over **6h** AND over **30m** | warning |
| `SLOErrorBudgetExhausted` | budget remaining ≤ 0 | warning |

Each paging alert requires **two windows** to breach simultaneously. The long
window proves the burn is real rather than a blip; the short window proves it is
still happening, so the alert clears promptly instead of hanging around for an
hour after the incident ends.

This is what static thresholds cannot do. `error rate > 1%` fires identically
for a 90-second wobble and a four-hour outage. A burn-rate alert fires in
proportion to how much of the budget the event is actually eating, which is the
same thing as how much your users are actually suffering.

---

## 5. The compliance window

**28 days.** Long enough that a single incident does not dominate it, short
enough to still reflect current reality. Recorded once, in one place:

```yaml
- record: slo:compliance_window_bad:ratio
  expr: slo:sli_bad:ratio_rate28d
```

Change the window by repointing that one rule (and adding the matching ratio
rule for the new window).

Two prerequisites that are easy to skip:

**Retention must exceed the window.** Prometheus cannot answer a 28-day question
with 15 days of data — the budget silently recovers as the retention window
rolls past the incident that spent it.

**Storage must be durable.** A budget that resets when the monitoring stack
restarts will report perfect attainment through an outage it has forgotten. If
you take one thing from this guide: an error budget is only as trustworthy as
the storage underneath it.

**Budgets need the window to fill.** For the first 28 days after deployment the
numbers are computed over whatever data exists, so early readings swing hard.
That is another reason to run recording-only for two weeks before routing pages.

## 6. Reading the board

**Error budget remaining** — the headline. Green above 50%, amber below 25%,
red once negative.

**SLO board table** — one row per SLO, sorted by budget remaining, so whatever
is worst is at the top. `persistence_availability` contributes one row per
service role.

**Burn rate panels** — the 1h view for "are we in trouble", the 5m-vs-1h view
for "is it still happening".

**A row showing `NaN` is correct, not broken.** The `server` role often has no
persistence traffic in a quiet window, and you cannot compute a success ratio
over zero events. An SLI with no traffic has no value — displaying a
confident 100% there would be a lie.

---

## 7. The gap worth knowing about

**Orphaned Task Queues are slow to show up in an SLO.** Work sent to a queue
nobody polls does not fail — it waits, and only becomes a bad event once it
passes `WorkflowExecutionTimeout`. If your timeout is an hour, your SLO is blind
to a total outage of that queue for an hour.

Request-outcome SLIs can only see an outcome once there is one. That is why
`TemporalTasksWithNoPoller` exists in `alerts.yml` as a plain threshold alert
alongside the budgets: it is cluster-side, fires immediately, and has no
false-positive mode. **Not everything worth paging on is expressible as an error
budget**, and an SLO-only alerting strategy has exactly this hole in it.

The same reasoning applies to a dead Worker fleet: SDK metrics stop existing, so
no threshold on them can fire. Absence has to be asserted explicitly, and the
cluster-side signals are the backstop.
