# Minimum Observability Standard — Temporal Application Teams

For teams **building Workflows on someone else's Temporal platform.** You do not
run the Temporal Service. You run Workers and you own your Workflow code, and
this is the smallest set of things you must be able to see.

Deliberately minimum. A team that maintains five signals is far better off than
one that inherits fifty and reads none.

```
prometheus/
  slo-rules.yml       3 SLIs, 28-day error budgets, 2 burn alerts (24 rules)
  alerts.yml          5 alerts — 4 active, 1 (absence) ships commented out
grafana/dashboards/
  temporal-app-team.json    one screen
scripts/
  conformance-check.sh      automated pass/fail against your namespace
```

---

## Adopt it in an afternoon

```bash
# 1. Point the rules at your namespace and task queue
./scripts/configure.sh my-team-prod orders

# 2. Load them (written to ./configured/, templates untouched)
cp configured/*.yml /etc/prometheus/ && curl -X POST http://prometheus:9090/-/reload

# 3. Import the dashboard, then prove it works
NAMESPACE=my-team-prod TASK_QUEUE=orders ./scripts/conformance-check.sh
```

`configure.sh` also **uncomments `AppWorkerFleetAbsent`** — the alert nothing
else can replace and the step most likely to be skipped — and validates the
result with `promtool` when it is available.

It exists because doing step 1 by hand is a trap: `sed -i 's/…'` **fails on
macOS**, where BSD sed reads the next argument as a backup suffix. The script
substitutes in Python instead, so it behaves the same everywhere.

The conformance check exits non-zero until you are conformant, so it works as a
CI gate or as something your platform team runs against you.

---

## The standard: five things you must be able to answer

| # | Question | Signal | Required |
|---|---|---|---|
| 1 | Are my Workers exporting metrics at all? | `temporal_worker_task_slots_available` exists | **yes** |
| 2 | Are my Workflows finishing? | failed ÷ (completed + failed) | **yes** |
| 3 | Are my Workers keeping up? | Activity schedule-to-start p99 | **yes** |
| 4 | Am I out of capacity? | task slots used vs available | **yes** |
| 5 | **Are my Workers alive at all?** | `absent()` on your task queue | **yes** |

Requirement 1 is the prerequisite: **no amount of Prometheus configuration
creates SDK metrics.** They come from your application code. If you have not
wired a metrics handler into your Worker, nothing else in this document can
work.

Requirement 5 is the one teams skip and the one that matters most — see below.

---

## Four things that will catch you out

Every one of these was measured against a real Worker, not inferred.

### Your metrics do not go to zero when your Workers die. They disappear.

When your fleet stops, `temporal_worker_task_slots_available` stops existing.
Every threshold alert you own goes quiet at exactly the moment you most need
one — `slots == 0` matches nothing when there is no series to match.

Only an **absence** alert catches this, and it must be scoped to your namespace
and task queue. A bare `absent(temporal_worker_task_slots_available)` never
fires on a shared platform: other tenants' Workers, and Temporal's own internal
Workers, keep the metric alive forever.

That is why `AppWorkerFleetAbsent` exists, and why the conformance check fails
if you have not enabled it.

### Your failure metrics do not exist until you fail

`temporal_workflow_failed_total`, `temporal_activity_execution_failed_total` and
`temporal_request_failure_total` are **absent** on a Worker that has never had a
failure. Verified on a healthy Worker: all three missing.

Two consequences:

- An alert written as `failed / completed` returns **nothing** when the failure
  series is missing — including the case where everything is failing and there
  are no successes. The shipped rules use an `or … * 0` guard for this.
- A dashboard panel that is empty is not proof of health. It may just be unborn.

### Your SLO threshold must be a bucket that actually exists

`le` is a **string match**. The SDK emits `le="1.0"`, so a rule written `le="1"`
matches nothing and the SLI produces **no series at all** — not a wrong number,
no number. This cost real debugging time while writing these rules.

Worse: the SDK's default histogram buckets are

```
0.001 0.002 0.005 0.01 0.02 0.05 0.1 0.2 0.5 1.0 2.0 5.0 10.0 +Inf
```

They **top out at 10 seconds**. A 60-second latency SLO is not expressible
against the defaults — you must configure custom buckets in your Worker first.
`conformance-check.sh` verifies your chosen boundaries exist and prints the real
ones if not.

### Activity failures are not Workflow failures

A healthy Temporal application absorbs Activity failures through retries.
Alerting on Activity failure count guarantees false pages. Only a terminal
**Workflow** outcome is a signal — which is what requirement 2 measures.

The corollary bites during incidents: a saturated fleet produces **timeouts**,
not failures. If your platform exposes cluster-side `workflow_timeout` for your
namespace, count it as bad; on SDK metrics alone you will see the work simply
stop rather than fail.

---

## Scope everything to your namespace

On a shared platform Prometheus you can see every tenant's series. An unscoped
rule pages you for someone else's incident, and an unscoped SLO quietly measures
their Workflows instead of yours.

Every rule here carries `namespace="$NAMESPACE", task_queue="$TASK_QUEUE"`, and
the conformance check warns when it detects more than one namespace in your
Prometheus.

---

## What to ask your platform team for

Three things you cannot produce yourself, in priority order:

1. **The cluster-side view of your namespace** — `workflow_success`,
   `workflow_failed`, `workflow_timeout`, `no_poller_tasks` filtered to your
   namespace. These come from the Temporal Service, so **they keep reporting
   when your Workers are down.** They are the backstop for the blind spot above,
   and `no_poller_tasks` is the only signal with no false-positive mode.
2. **Node metrics for your Worker hosts** (CPU/memory). Without them, "we need
   more Workers" and "our Workers are configured too small" are
   indistinguishable — identical schedule-to-start curves, identical slot
   exhaustion, opposite fixes.
3. **Your namespace's rate limits and current usage.** Being throttled looks
   exactly like your code being slow.

---

## The objectives are placeholders

99% completion, 99% task delivery, 95% latency are round numbers, not
measurements. Run in recording-only mode for two weeks, look at what you
actually deliver, then promise that.

An SLO you miss at baseline is worse than no SLO: it teaches the team to ignore
the board inside a week.

---

## Verified

The rules were loaded into a live Prometheus against a running Worker and
confirmed to evaluate — all three SLIs produce series with sane values. The
conformance check was exercised in both directions: exit 1 when rules are
missing or the absence alert is disabled, exit 0 once configured, and it
correctly rejects a bucket boundary that does not exist.

The dashboard's queries are validated; its rendering has not been reviewed by
eye.
