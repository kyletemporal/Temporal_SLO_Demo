# Worker scale testing

How much throughput does one more Worker actually buy you? This document is the
methodology behind `scripts/scale-benchmark.sh`, the arithmetic you should do
before running it, and the measured result from this stack — which turned out
to be a *negative* one, and is more useful for it.

Everything here runs on the compose stack with no API keys and no cloud
account: `make scale-benchmark`.

---

## 1. The question this answers

[`RECOMMENDATIONS.md`](../../RECOMMENDATIONS.md) §4 documents a decision tree
for a rising schedule-to-start. Two causes look **identical** on the dashboard:

- too few Workers, or
- each Worker configured too small (`MAX_CONCURRENT_ACTIVITIES`).

The tree splits them on host CPU. That is the correct diagnosis and it is still
a diagnosis made mid-incident, from a dashboard, under pressure, by whoever is
on call.

This benchmark answers the same question **in advance and in numbers**. Adding
a Worker either raises sustained throughput or it does not. If it does not, the
bottleneck is not fleet size, and no autoscaler will fix it — an autoscaler
will make it worse, which is scenario 8 (poller flood).

---

## 2. The arithmetic, before you run anything

Do this first. If the prediction and the measurement disagree, one of them is
wrong and finding out which is the entire value of the exercise.

**A slot is held for the duration of the Activity, not for the duration of the
Workflow.** So the unit that matters is *slot-seconds per Workflow*, not
activities per Workflow.

`OrderWorkflow` runs three Activities on the clean path:

| Activity | Work | Slot-seconds held |
|---|---|--:|
| `ValidateOrder` | returns immediately | ~0 |
| `ChargePayment` | sleeps `ACTIVITY_DELAY_MS` | 0.050 |
| `ShipOrder` | returns immediately | ~0 |

At the compose default of `MAX_CONCURRENT_ACTIVITIES: "10"` and a 50 ms delay:

```
one slot          = 1000 / 50          =  20 delayed Activities/sec
one Worker        =  10 slots × 20     = 200 delayed Activities/sec
slot ceiling      = 200 Workflows/sec per Worker   (one delayed Activity each)
```

Then invert it with Little's law to predict occupancy at a given offered load:

```
slots busy (avg)  = Workflows/sec × slot-seconds per Workflow
                  = 80/s × 0.050s  = 4.0 of 10 slots
```

**A single Worker should absorb 80/s with six slots to spare.** That is the
prediction. Hold onto it — §5 is what actually happened.

The general form, for your workload:

```
slot-seconds per Workflow = Σ (p95 duration of each Activity)
Workflows/sec per Worker  = MAX_CONCURRENT_ACTIVITIES / slot-seconds per Workflow
Workers needed            = target Workflows/sec / the above
```

Use p95 Activity duration, not the mean. Slots are held by the slow executions,
and a mean that includes a fleet of fast ones will under-size you by the exact
factor you can least afford.

---

## 3. Running it

```bash
make scale-benchmark                    # 1, 2, 4 Workers @ 40/s, 3m steps
make scale-benchmark-quick              # 1, 2 Workers @ 2m — shape only

# Or drive it directly:
WORKERS="1 2 4 8" RATE=120 DURATION=5m WARMUP=90 \
  bash scripts/scale-benchmark.sh
```

| Variable | Default | Notes |
|---|---|---|
| `WORKERS` | `1 2 4` | Replica counts to sweep, in order |
| `RATE` | `40` | Offered Workflows/sec. **Integer only** — k6 aborts on a fraction |
| `DURATION` | `3m` | Per step. Must exceed `WARMUP` by ≥ 60s |
| `WARMUP` | `60` | Seconds discarded from the front of each step |
| `ACTIVITY_DELAY_MS` | `50` | `ChargePayment` sleep. Non-zero is load-bearing — see §6 |
| `PROM` | `http://localhost:9090` | Where the measurements come from |
| `OUTDIR` | `./scale-results` | CSV, markdown report and per-step k6 logs |

**Pick `RATE` so the first row falls short of it.** A sweep where one Worker
already sustains the offered load measures nothing — every later step has
nothing left to improve, and the table is flat by construction. The report says
so explicitly when it detects this rather than presenting a flat line as a
finding.

Afterwards, put the fleet back:

```bash
docker compose rm -sf worker && docker compose up -d worker
```

The board that renders the same signals live, while a sweep is running:
**Workers — Scale & Capacity** (`grafana/dashboards/custom/worker-scale.json`,
uid `temporal-worker-scale`).

---

## 4. Reading the table

| Column | Reads as |
|---|---|
| Throughput below offered | The fleet cannot keep up. The gap is backlog being built. |
| S2S p95 rising | Tasks are waiting for a poller. The first symptom of saturation. |
| Min slots free at 0 | **Slot-bound.** More Workers will not help; raise concurrency. |
| Min slots free high, S2S high | Worker-bound or server-bound, **not** slot-bound. |
| Sync match falling | The queue stopped draining instantly — tasks now round-trip persistence. |
| Poll success falling | More pollers than work. Over-provisioned (scenario 8). |
| Efficiency below ~0.8 | Diminishing returns. Near the knee of the curve. |

**Efficiency** is throughput-per-Worker normalised to the first step. `1.00`
means this step converted added Workers into added throughput as well as the
first one did. `0.50` means each added Worker is buying half as much. The knee
of the curve is where this starts falling, and it is the number to take to a
capacity conversation.

The one to read first is **min slots free**. It is a *minimum over the window,
not a mean* — slot exhaustion is a transient that a mean hides completely. A
fleet that hits zero for two seconds in every ten has a comfortable average and
is dropping throughput on the floor.

---

## 5. What this stack actually measured

Run of 2026-09-16, `WORKERS="1 2 4" RATE=80 DURATION=2m WARMUP=60
ACTIVITY_DELAY_MS=50` — full report in
[`scale-results/scale-20260916-090651.md`](../scale-results/scale-20260916-090651.md).

| Workers | Offered /s | Throughput wf/s | S2S p95 (s) | S2S p99 (s) | Min slots free | Sync match | Efficiency |
|--:|--:|--:|--:|--:|--:|--:|--:|
| 1 | 80 | 73.82 | 0.2030 | 0.4406 | 2 of 10 | 0.1667 | 1.00 (base) |
| 2 | 80 | 70.18 | 0.1739 | 0.3292 | 12 of 20 | 0.2280 | 0.48 |
| 4 | 80 | 70.12 | 0.2905 | 2.8411 | 33 of 40 | 0.2983 | 0.24 |

**Throughput did not scale.** 73.8/s → 70.1/s while the fleet grew 4x.
Efficiency 1.00 → 0.24: the fourth Worker bought nothing at all.

**Nor was the fleet slot-bound.** §2 predicted 4 of 10 slots busy at 80/s, and
Little's law on the measured numbers agrees — 73.8 Workflows/sec × 0.050
slot-seconds ≈ 3.7 slots busy. At four Workers, 33 of 40 slots were free at the
*worst* sampled moment. Slots were never the constraint.

**And it was not the load generator.** k6 held the offered rate at every step —
`79.99999/s`, `79.994057/s`, `79.958384/s` — with **0 failed requests and no
dropped iterations**
([`scale-results/`](../scale-results/)).

That same k6 summary carries the corroborating detail. Its own
`http_req_duration` p95 on `POST /orders` went **13 ms → 27 ms → 49 ms** as the
fleet grew 1 → 2 → 4. The *start* path got slower as Workers were added. Nothing
a Worker does should slow down Workflow creation — unless the added Workers are
loading the same component the starts go through. They are: the Frontend, and
the persistence behind it.

So: not slots, not Workers, not the generator. What is left is the server, and
the column that fingerprints it is **sync match rate — 0.17 to 0.30**. Between
70% and 83% of Tasks failed to hand off to a waiting poller and round-tripped
**persistence** instead. On this stack that is one Postgres container serving a
single Temporal container running all four roles beside Prometheus, Grafana,
Loki, Tempo, Pyroscope and k6.

The p99 schedule-to-start makes the same point from the other end: **0.44s at
one Worker, 2.84s at four.** Queue wait got *worse* as the fleet grew, because
more pollers against a persistence-bound matching service adds poll load
without adding drain capacity.

**This is the negative result the benchmark exists to produce.** An HPA on
schedule-to-start, pointed at this workload, would have watched S2S climb from
0.203s to 0.290s, scaled up, made it worse, and scaled up again. The
autoscaling config in [`aws/k8s/worker/hpa.yaml`](../../aws/k8s/worker/hpa.yaml)
is correct *for a fleet-bound workload* — and the only way to know you have one
is to measure it before you turn the autoscaler on.

### A second run, at half the load

2026-09-28, `WORKERS="1 2" RATE=40 DURATION=2m` —
[`scale-results/scale-20260928-104405.md`](../scale-results/scale-20260928-104405.md):

| Workers | Offered /s | Throughput wf/s | S2S p95 (s) | Min slots free | Sync match |
|--:|--:|--:|--:|--:|--:|
| 1 | 40 | 36.04 | 0.2886 | 3 of 10 | 0.3880 |
| 2 | 40 | 36.16 | 0.1710 | 16 of 20 | 0.6156 |

Same shape at half the offered load, and a cleaner illustration of why
schedule-to-start alone is a trap. The second Worker **halved queue wait**
(0.289s → 0.171s) and **improved sync match by half again** (0.39 → 0.62) while
moving throughput by 0.1/s. The fleet got measurably better at picking work
*up* and no better at getting work *done* — because throughput was pinned ~10%
below the offered rate by something that is not the fleet, in both rows.

That is the pattern an HPA on schedule-to-start handles worst: it scales up,
the signal it watches recovers, it scales back down, and the throughput deficit
is exactly where it started.

> These are laptop numbers. **Read where the curve bends, not the absolute
> values.** The methodology transfers; the figures do not.

---

## 6. Why the harness is built the way it is

Five decisions, each of which was a wrong answer first.

**Open loop, not VUs.** `k6/09-scale-sweep.js` uses `constant-arrival-rate`. A
VU-based test is *closed-loop*: each VU waits for its request before sending
the next, so when Workers slow down the generator slows with them. Offered load
becomes a function of the system's own capacity, throughput reads flat no
matter what you change, and the backlog that would reveal the bottleneck never
forms. This is the single most common way a Temporal load test produces a
meaningless number.

**A non-zero Activity delay.** With instant Activities a laptop saturates on
Temporal's RPC path long before slots matter, and the sweep measures the server
instead of the fleet. 50 ms gives the arithmetic in §2 a real quantity to
predict and be checked against.

**`WARMUP` is not padding.** Three things corrupt the first seconds of a step:
Prometheus's `dns_sd` has a 10s refresh so new Workers are not yet scraped;
`rate()` over a partially-filled window under-reports; and a fleet that was
behind spends the first seconds draining the backlog from the *previous* step —
which reads as throughput *above* the offered rate.

**`docker compose rm -sf worker` before each step.** `--scale worker=1` leaves
surplus containers **stopped, not removed**, and a later compose command
restarts them. Measured once as 11 running Workers during a step that was
supposed to have 1, silently invalidating every row after it.

**The prose has to agree with itself.** The report's two narrative paragraphs
— one keyed on throughput, one on schedule-to-start — were written
independently, and on the 2026-09-28 run they landed on opposite advice four
lines apart: *"an autoscaler would make this worse"* followed by
*"schedule-to-start is the right metric to autoscale on for this workload"*.
Both sentences were individually defensible against their own column. The pair
was worthless. The schedule-to-start branch now reads the throughput verdict
first, because whether falling queue wait is good news depends entirely on
whether throughput moved with it.

**Report the Workers Prometheus can see, not the number requested.** The CSV's
`workers` column is `count(up{job="temporal-sdk-worker"} == 1)`. These diverge
when `dns_sd` has not caught up or a container failed to start, and trusting
the requested count is how a benchmark reports per-Worker throughput against a
fleet size that never existed.

---

## 7. Metric traps this shook out

All three shipped, ran green, and produced numbers that were wrong rather than
missing. They are documented here because every one of them is reusable — the
same traps sit under any PromQL you write against this SDK.

**`temporal_activity_execution_latency_count` has zero series.** The name
appears in the metric index, so a query using it parses, returns no data, and
prints as a harmless `n/a`. The Go SDK emits durations in seconds and the
naming scope appends the suffix, so the histogram's count is
**`_seconds_count`**. Cost: one entire benchmark run with a blank Activities
column (see the `n/a`s in
[`scale-20260915-135249.csv`](../scale-results/scale-20260915-135249.csv)).

**`min_over_time()` over an aggregation needs subquery syntax.** `[60s]` on a
non-selector is a *parse error*, not a zero — and the harness turns any query
error into an empty string, which prints as `n/a`. It needs `[60s:15s]`.

**`temporal_worker_task_slots_available` includes Temporal's own internal
Workers.** They run inside the server container with 1000 slots per internal
task queue. The first corrected run reported **8110 free slots for a fleet that
had 40**. There is no `namespace` label to exclude on here — the filter that
works is `job="temporal-sdk-worker"`. Same trap as the `temporal_system`
namespace exclusion applied everywhere else in this repo, on a metric that
needs a different lever to avoid it.

---

## 8. Taking it to a real environment

The methodology holds; four things change.

1. **Fill in the CPU column.** It reports `n/a` here because there is no
   cAdvisor on the compose stack — deliberately, rather than a fabricated zero
   that would read as "idle" in the one column that separates CPU-bound from
   slot-bound. With cAdvisor or `kubelet` cgroup metrics present, the existing
   query populates it.
2. **Separate the load generator from the system.** k6, Prometheus and Temporal
   sharing a laptop is why §5 lands on the server. In a real test they are on
   different hosts, and the negative result may not reproduce.
3. **Sweep further than 4.** The knee is the output. 1, 2, 4, 8, 16 with
   `DURATION=5m` costs half an hour and answers the capacity question for a
   quarter.
4. **Watch Workflow Task slots too.** This harness measures
   `worker_type="ActivityWorker"` only. `MAX_CONCURRENT_WORKFLOW_TASKS`
   defaults to 10 alongside it, and a Workflow-Task-slot-bound fleet presents
   exactly like the Activity-bound case on every panel here. Swap the
   `worker_type` label to check it.

---

## 9. What this does not measure

- **Production capacity.** Laptop numbers. Shape, not magnitude.
- **Workflow Task slot pressure** — §8.4.
- **Sustained load.** The longest step here is minutes. Nothing about this
  catches a leak, a cache that degrades over hours, or a history that grows
  until Workflow Task latency does.
- **Cold start.** Every step scales a fleet that is already warm. Scale-from-
  zero is a different measurement.
- **Multi-Namespace or multi-task-queue interference.** One Namespace, one task
  queue, one Workflow type.

---

## See also

- [`RECOMMENDATIONS.md`](../../RECOMMENDATIONS.md) §4 — the decision tree this
  benchmark answers in advance
- [`docs/CHAOS-RUNBOOK.md`](CHAOS-RUNBOOK.md) scenario 8 — poller flood, what
  over-provisioning looks like on the dashboards
- [`aws/k8s/worker/hpa.yaml`](../../aws/k8s/worker/hpa.yaml) — autoscaling on
  schedule-to-start, correct *only* for a fleet-bound workload
- [`docs/SLO-GUIDE.md`](SLO-GUIDE.md) — where schedule-to-start sits in the SLI set
