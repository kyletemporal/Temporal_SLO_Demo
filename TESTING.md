# Testing & Feedback

What has actually been verified, what has not, and the known gaps. Please read
the "not verified" list before filing anything — some of it is already known.

---

## Start here

```bash
cd demo
./deploy.sh --clean      # ~5 min on first build
make validate            # 26 checks: containers, targets, rules, every panel, SLOs
```

`make validate` should end `passed: 26  failed: 0`. If it does not, that is the
most useful bug report you can send — paste the whole output.

Then open Grafana and drive it:

```bash
make chaos-slots         # ~6 min. Watch worker_task_delivery drain on the SLO board
make chaos-orphan        # ~4 min. Tasks With No Poller. Nothing errors; work stops
make chaos-blackout      # ~4 min. Worker fleet dies. SDK panels go BLANK, not red
```

---

## Verified working

Run end to end, more than once:

- Clean build from scratch (`down -v` → `deploy.sh`), and re-run idempotency
- All 4 Prometheus targets up; all 67 rules load healthy
- **Every panel query on all three dashboards returns data** (or is empty by
  design — the validator has an allowlist with reasons)
- Scenario 0 (baseline), 3 (orphan), 4 (slot saturation), 5 (blackout)
- `make dashboards` — community import, with the Datadog file and the
  SDK-flavour mismatch filtered out
- Error budget burn proven: `chaos-slots` took `worker_task_delivery` from
  +44% to −13% and put `SLOErrorBudgetExhausted` into pending
- `TemporalWorkerFleetAbsent` observed going `inactive → pending → firing`
  while the fleet was stopped, and the naive unscoped `absent()` observed
  staying silent through the same outage
- `TemporalWorkflowFailureRatioHigh` fired after injected failures
- Blackout signature confirmed: SDK metrics went **absent**, not zero;
  **nothing failed or timed out** during the outage (durability holds); the
  backlog drained on recovery

## NOT verified — please poke at these

- **Scenarios 1 and 2 have never been run end to end.** Only parse-checked.
  `chaos-backlog` has been run once ad hoc; `chaos-failures` never.
- **`SLOFastBurn` / `SLOSlowBurn` have never fired.** Only
  `SLOErrorBudgetExhausted` has. A 14.4x burn needs a sharper fault than slot
  saturation produces.
- **No visual check of Grafana rendering.** Every query is validated; whether
  panels *draw* correctly is not. The SLO board's table joins four queries with
  a `merge` transformation — the most fragile thing here. **Please eyeball that
  table specifically.**
- `make scale-up`, `make scale-reset`, `make deps-refresh` never run.
- The `production/` bundle's rules were loaded into a live Prometheus and
  confirmed evaluating, but the bundle has never been deployed to a real
  multi-namespace cluster.
- Everything was run on **macOS + Docker Desktop, arm64**. Linux and x86
  untested.

## Known gaps and open questions

- **Prometheus has no durable volume.** `make down` erases every error budget.
  Fine for a lab, wrong for anything real, and called out in the docs.
- **Scenario 2 Phase A is miscalibrated.** It injects `failureRate: 0.6` with
  `maxAttempts: 3`, so ~22% of Workflows fail outright — in the phase the
  runbook says *not* to page on. Dropping Phase A to `0.3` gives ~2.7% and the
  contrast the scenario is trying to teach. One line in
  `k6/02-failure-injection.js`. **Deliberately left alone — opinions welcome.**
- **One unresolved observation.** During a scripted `make chaos-blackout`,
  `absent()` was true for ~180s but no `ALERTS` series was recorded for
  `TemporalWorkerFleetAbsent`, even though the same rule was directly observed
  going pending→firing when the fleet was stopped by hand, twice. The rule is
  loaded, healthy, and correct. Not explained. If you can reproduce it either
  way, that is valuable.
- **Orphaned queues are slow to hit an SLO.** Work sent to an unpolled queue
  does not fail, it expires — so it only becomes a bad event after
  `WorkflowExecutionTimeout` (10 min here). A default 4-minute
  `make chaos-orphan` finishes before anything times out, and the SLO board
  stays green through a total failure to do work. `TemporalTasksWithNoPoller`
  covers it instantly; not everything worth paging on fits in an error budget.
- **No node-level metrics.** Without Worker host CPU, "we need more Workers" and
  "our Workers are configured too small" are indistinguishable — identical
  schedule-to-start curves, identical slot exhaustion, opposite fixes.
  `production/prometheus/prometheus.example.yml` includes the scrape job; the
  demo does not run it.

---

## Things that will look broken and are not

Worth knowing before you file these:

- **Four panels are empty on a healthy system**: *Tasks With No Poller* (its
  title says expect zero) and the failed/timeout/cancel series of *Workflow
  Outcomes*. The validator allowlists them with reasons.
- **A `no traffic` tile on the SLO board.** The `server` role often has no
  persistence traffic in a quiet window, and you cannot compute a success ratio
  over zero events. An SLI with no traffic has no value; showing a confident
  100% there would be a lie.
- **`worker_task_delivery` has a 90% objective, not 99%.** The demo pins
  `MAX_CONCURRENT_ACTIVITIES=10` so slot exhaustion is reachable on a laptop;
  that config delivers 94.4% within 200ms. Raise it to 200 and the SLI goes to
  ~100%.
- **`make verify` reporting `poll_success_sync` not yet emitted** on a fresh
  stack. It is a counter — it does not exist until traffic produces a sync
  match. Run `make smoke` first.
- **Community dashboards with empty panels.** Failure counters that have not
  fired yet, plus Java-only and local-activity metrics this Go app never emits.
- **Grafana logging errors about `provisioning/plugins` and
  `provisioning/alerting`.** Optional directories this stack does not use.

---

## Useful feedback

Most valuable, roughly in order:

1. `make validate` failing, with full output and `docker compose ps`
2. Anything in "not verified" that breaks — especially the SLO board table
   rendering and scenarios 1 and 2
3. An SLI you think measures the wrong thing. The definitions matter more than
   the objectives, and they are the part meant to be copied into real clusters
4. Objectives that are obviously wrong for your hardware
5. Anything in the docs that is confidently stated and false

---

## `cloud/` — unvalidated against a live account

The Cloud bundle was built from Temporal's published SLA and OpenMetrics
reference. It has **never been pointed at a real Temporal Cloud Namespace.**

Verified: `promtool check rules` passes on both files (39 + 11 rules), every
PromQL expression in the rules and the dashboard parses against a real
Prometheus, the dashboard has zero layout overlaps, and no expression applies
`rate()` to a Cloud counter (which would be wrong — they are gauges holding
pre-computed rates).

Not verified: any actual value. If you have a Cloud account, the highest-value
feedback is:

1. Do the metric names and labels match what your endpoint actually serves?
2. Does `cloud_service_availability` produce a plausible number, and how far is
   it from the figure Temporal reports? It should read *worse* — the metric has
   no error-type label, so SLA-excluded errors cannot be filtered out.
3. Do the limit gauges (`temporal_cloud_v1_action_limit` and friends) appear for
   your plan? The saturation panels and three alerts depend on them.
4. Is `temporal_cloud_v1_replication_lag_p99` present? It should exist only on
   High Availability Namespaces.
5. Does the opt-in `temporal_activity_type` label need enabling on your scrape
   URL for the Activity latency panel to populate?

---

## Landmine sweep (2026-08-12)

An adversarial pass over the whole repo. Six issues found and fixed; two
documented as accepted risks.

**Fixed**

1. **`production/alerts.yml` paged permanently if deployed unedited.**
   `absent()` on a selector matching nothing returns 1, so
   `TemporalWorkerFleetAbsent` with `REPLACE_ME` still in it fired immediately
   and forever on a healthy cluster. Now ships commented out with instructions —
   the fastest way to get a rule set muted is to page someone on day one with an
   alert that was never true.
2. **`make validate` failed on an idle stack.** Schedule-to-start histograms do
   not exist until an Activity has run, so a freshly deployed stack reported
   four panels as broken. The validator now detects whether traffic has flowed
   and downgrades those to WARN with instructions. Verified in three states:
   passes with traffic (26/0), warns when idle, still FAILS on a genuinely
   broken query.
3. **The traffic gate initially used `rate()`** and hit the same first-sample
   bug documented in the SLO guide — a counter that has just appeared has no
   earlier sample, so `rate()` returned 0 and the gate reported "no traffic"
   right after the first Workflow. Now tests presence.
4. **Duplicate Grafana dashboard UIDs.** `demo/` and `production/` shipped
   identical `uid` values, so provisioning both into one Grafana collides.
   Production dashboards are now `*-prod`.
5. **Cloud record names collided with self-hosted.** Both recorded `slo:*` with
   overlapping `sli` labels — a team running self-hosted *and* Cloud in one
   Prometheus would have silently merged the two into one number. Cloud is now
   prefixed `cloudslo:*`.
6. **`deploy.sh` port check silently skipped on Linux.** `lsof` is absent from
   many images and `lsof … >/dev/null 2>&1` exits non-zero when the binary is
   missing exactly as when the port is free, so the whole loop passed. Now falls
   back to `ss`/`netstat` and warns if none exist.

**Accepted, documented**

- `demo/` and `production/` still share `slo:*` record names. They are
  alternatives — you would not load both — and the READMEs now say so
  explicitly.
- `validate.sh` no longer asserts exact rule counts. Counts are generated and
  change legitimately when you edit an SLI list; it now checks that every
  expected group loaded and every rule evaluates.

**Also verified clean**: no secrets or absolute paths committed, all shell and
Python files parse, every relative markdown link resolves, `.gitignore` behaves,
all six rule files pass `promtool`, and every generator is idempotent and
reproduces its committed output.

---

## `app-team/` — the minimum standard

Rules were loaded into a live Prometheus against a running Worker and confirmed
to evaluate: all three SLIs produce series with sane values. The conformance
check was exercised in both directions — exit 1 when rules are missing or the
absence alert is disabled, exit 0 once configured — and it correctly rejects a
bucket boundary that does not exist.

**Two bugs were caught by running it**, both worth knowing because they are
silent:

- `le` is a **string match**. `le="1"` does not match the SDK's `le="1.0"`, and
  the SLI produced **no series at all** — not a wrong number, no number.
- The SDK's default histogram buckets **top out at 10s**, so the original 60s
  latency objective matched nothing. `conformance-check.sh` now verifies both
  boundaries exist and prints the real ones when they do not.

Not verified: dashboard rendering (queries all parse, 14/14), and the bundle has
never been used by an actual second team on a shared platform Prometheus —
the multi-tenant scoping is reasoned, not observed.

---

## Landmine sweep #2 (2026-08-13) — after `cloud/` and `app-team/`

**Fixed**

1. **`make validate` failed on a freshly-restarted healthy stack.** The
   golden-signals dashboard added empty-by-design panels whose titles did not
   match the allowlist — `Workflow outcomes` vs `Workflow Outcomes`, `Tasks with
   no poller` vs `Tasks With No Poller (expect zero)`, plus `Server-fault rate
   by type`, which is empty precisely when nothing is wrong. The earlier run
   passed only because recent chaos had left data in the window. Matching is now
   case-insensitive and the missing panels are allowlisted with reasons.
   Re-verified: 24/0 on a healthy stack, still FAILS on an injected bad query.
2. **`sed -i 's/…'` in the app-team docs fails on macOS.** BSD sed reads the
   next argument as a backup suffix, so the documented *first step of adoption*
   breaks on the platform most likely to be used. Replaced with
   `app-team/scripts/configure.sh`, which substitutes in Python, **uncomments
   `AppWorkerFleetAbsent`** (the step most likely to be skipped), and runs
   `promtool` when available. Verified end to end: 4 alerts became 5, zero
   leftover placeholders, promtool clean.
3. **Alert-name collisions between `production/` and `cloud/`.** Both defined
   `SLOFastBurn`, `SLOSlowBurn`, `SLOBudgetBurnTicket` and
   `SLOErrorBudgetExhausted` — and the docs explicitly bless running those two
   bundles in one Prometheus. An on-call would have seen `SLOFastBurn` with no
   way to tell which stack fired. Cloud's are now `CloudSLO*`.
4. **Stale counts and prose.** `production/README` claimed 10 alerts after one
   was commented out (now 9 + 1 commented); the root README said "Three bundles"
   with four present, and repeated the old count.

**Verified clean this round**

No secrets or absolute paths; all shell and Python parses; every relative
markdown link resolves; no duplicate dashboard UIDs; zero record-name collisions
involving `cloud/` or `app-team/`; all eight rule files pass `promtool`;
generators idempotent. The shipped, *unsubstituted* app-team files pass
`promtool` and are **silently inert** rather than firing spuriously — the safe
failure mode, and `conformance-check.sh` catches the silence.

**Accepted**

`demo/` and `production/` still share 14 `slo:*` record names. They are
alternatives, never both, and both READMEs say so.

---

## Log checks in `make validate` (2026-08-14)

Section 6 covers Loki. Adding the log panels initially **broke** the validator —
it sent LogQL to Prometheus, producing two false "NO DATA" and one HTTP 400.
Panels are now routed by their datasource type.

The three log panels are allowlisted as empty-by-design (no errors, nothing
stuck — the good outcomes), which is only safe because section 6 proves the
pipeline independently:

- **liveness over a 5-minute window**, not presence over an hour
- `temporal` and `worker` logs both arriving
- `level` label populated (a broken regex silently voids every severity filter)
- worker logs carry `WorkflowID`
- `workflow_id`/`run_id` are **not** Loki labels — that would be a cardinality bomb

**Negative-tested.** Stopping Alloy with a one-hour window still passed, because
Loki keeps what was already shipped — a dead collector looked identical to a
healthy quiet system. The 5-minute liveness window catches it. Aggregate rather
than per-service, because the Temporal Service logs periodically and is a
reliable heartbeat while an idle worker is legitimately silent. Stopping Loki is
caught too.

---

## Cross-check against Joshua Smith's Temporal Cloud Observability deck (2026-08-14)

**Already aligned:** schedule-to-start p99 >200ms, sync match <95%, worker task
slots >0, poll-success SLI formula, resource exhaustion (cloud), backlog count
(cloud), SDK `request_failure` (app-team).

**Gaps closed:**

- `TemporalNonDeterminismError` — was ABSENT despite being the deck's CRITICAL
  page-a-human tier. Added to demo + production.
- `TemporalPollSuccessRateLow` — the SLI existed but nothing alerted on it.
- Sticky cache panel (deck WATCH tier); the metrics exist on this stack.
- TMPRL1100 / TMPRL1101 added to the stuck-executions LogQL.
- `make chaos-nde` / `chaos-nde-stop`.

**The deck's NDE rule is wrong on this SDK, and it matters.** It publishes

```
temporal_workflow_task_execution_failed_total{error_type="NonDeterminismError"}
```

Forcing a real NDE and reading the exported series gives:

```
temporal_workflow_task_execution_failed_total{failure_reason="NonDeterminismError",
  workflow_type="OrderWorkflow", task_queue="orders", client_name="temporal_go"} 30
```

The label is **`failure_reason`**, not `error_type`. The value is correct. Copied
verbatim, that rule matches nothing and yields a permanently silent alert for the
single failure Temporal cannot retry its way out of. Verify on your own SDK —
Java/TS/Python may differ again.

`[TMPRL1100]` was confirmed present in worker logs, carrying WorkflowID and RunID,
which is what makes the log panel a real path from alert to affected executions.

**Open, deliberately not changed:** frontend error threshold is `>1%` here; the
deck recommends alerting below 99.9% success (`>0.1%`), 10x tighter. Measured
actual on this stack is 0.000000%, so the tighter value would be quiet — but it
is a paging decision across three bundles, so it is flagged rather than switched.
Not implemented: failure-conversion-rate (`workflow_failed / activity_execution_failed`,
>0.1 poor / <0.01 good).

---

## `make chaos-stuck` — stuck Workflows (2026-08-14)

The lab had no scenario for the silent case. `chaos-orphan`, `chaos-blackout` and
`chaos-nde` all stall Workflows, but each trips an alert. Nothing exercised the
`monitor/` service's whole reason for existing.

Two modes:

- **`parked`** — awaits a Signal that never arrives. Verified over 25+ minutes:
  `workflow_success`, `workflow_failed`, `workflow_timeout` and `no_poller_tasks`
  all stayed at **0.0000**. Nothing fires, and nothing will. Every Prometheus
  counter here describes a Workflow that ENDED; one that never ends increments
  nothing.
- **`retry-storm`** — unlimited retries on an always-failing Activity. Visible as
  0.2/s activity failures, but `workflow_failed` stays flat, so the failure-ratio
  alert never fires while Actions burn indefinitely.

**The first version of this scenario taught the opposite of the truth.** The lab
sets `WorkflowExecutionTimeout: 10m` on every Workflow, so parked executions ended
**TimedOut** and burned error budget within minutes — measured, alerts firing.
An execution timeout *converts* an invisible stuck Workflow into a visible failed
one. `StuckMode` now runs with no execution timeout, which is Temporal's default
and the real exposure. Both generations are visible side by side in one query:
the capped run `TimedOut`, the uncapped run still `Running` at 25 minutes.

Attribution caveat learned here: burn alerts were already firing from the earlier
NDE experiment's timeouts an hour prior. Check `activeAt` before crediting a
scenario for an alert it did not cause.

Cleanup: `make chaos-stuck-release` (batch Signal + batch terminate). Required —
these Workflows have no timeout and will otherwise run until the stack is torn down.

---

## Validator was under-reporting its own warnings (2026-08-14)

`validate.sh` section 7 printed its own `PASS`/`WARN`/`FAIL` text from **inside a
Python heredoc**, bypassing `ok()`/`warn()`/`bad()` entirely. The summary read
`warnings: 0` while a WARN was plainly on screen two lines above it.

A summary that does not count what it displays is its own silent failure — in the
script whose entire job is catching those. Python now emits a `__VERDICT__:`
marker line and the shell does the accounting. Correct output is
`passed: 32  failed: 0  warnings: 1`, the warning being `worker_task_delivery`
budget exhausted, which is expected after a session of chaos runs.

Worth checking whenever a new section is added: if a check prints its own
verdict, it is not being counted.

---

## Four distribution decisions implemented (2026-08-17)

1. **Frontend error threshold 1% → 0.1%** across demo, production and cloud,
   matching Temporal's "<99.9% success" guidance. Measured actual on the demo
   stack first: 0.000000%, so it is quiet in steady state.
2. **`make verify-sdk-labels`** — checks the shipped alerts against *your* SDK's
   real label names and units, instead of a code comment nobody reads before an
   incident. It refuses to guess: the NDE label cannot be verified until a
   Workflow Task has actually failed, and it says so rather than passing.
3. **Named volumes for Prometheus and Loki**, and `make down` no longer passes
   `-v`. Error budgets are cumulative over 28 days and were being erased by a
   routine stop. `make reset` is now the destructive one.
4. **MIT licence + an explicit "community, not Temporal-supported" statement.**

### `chaos-nde` was not observable, which the verifier exposed

The scenario started its divergent Worker with `docker compose run`. Prometheus
finds Workers by `dns_sd` on the **`worker` service name**, and a `run` container
does not get that record — so the NDE metric never reached Prometheus and
`TemporalNonDeterminismError` could never fire. The scenario documented this as a
caveat; it was really a defect.

It now recreates the worker *service* with `NDE_INJECT=1`, keeping the DNS name.
Verified end to end for the first time: the metric arrives with
`failure_reason="NonDeterminismError"`, the alert reaches **firing**, and
`verify-sdk-labels` passes on both label and value.

---

## Tracing and profiling (2026-08-17)

OTel traces to Tempo via an OTel Collector; continuous profiling to Pyroscope;
pprof on its own listener. **Metrics stay on Tally** — per the FR, switching them
to OTel renames every series the rules and dashboards are built on.

**The replay question is answered: the interceptor does NOT re-emit spans on
replay.** Started a Workflow with a 40s Activity, restarted the Worker mid-flight
to force replay from history, compared before and after: one trace before, one
after, every span name exactly once. This was the open question that decided
whether tracing was affordable, and it was verified rather than taken from docs.

### Collector-side attribute normalisation

The SDK emits camelCase, non-conventional attributes — read off real spans rather
than assumed:

```
temporalWorkflowID / temporalRunID / temporalActivityID
```

The collector copies them to `temporal.workflow.id`, `temporal.run.id`,
`temporal.activity.id`, adds `temporal.span.kind` (workflow/activity) and
`temporal.span.phase` (start/run), and stamps `deployment.environment` and
`service.namespace` onto every span. Originals are kept, so dashboards written
against either form work. Doing it in the collector means every SDK gets it
without an application change.

Verified working: `{ span.temporal.workflow.id = "order-norm-1" }` returns the
right trace, and `{ span.temporal.span.kind = "activity" && duration > 50ms }`
returns slow activities.

### Log → trace without trace IDs in logs

The Go SDK logger does not inject trace IDs, so the usual `trace_id` derived
field has nothing to match. The **WorkflowID is the join key**: logs print it and
the normalised span attribute carries it, so a Loki derived field links to a
TraceQL query. The query is verified; the Grafana deep-link URL format is
version-sensitive and has not been clicked through in a browser.

### Two bugs found while building

- `resource.Merge(resource.Default(), resource.NewWithAttributes(semconv.SchemaURL, ...))`
  fails with **"conflicting Schema URL"** whenever the app's semconv version
  differs from the SDK's. `resource.NewSchemaless` merges with anything. Tracing
  failed open exactly as designed — the Worker kept polling — which is why it was
  a log line rather than an outage.
- pprof could not mount on the metrics port: the tally Prometheus reporter owns
  that server and exposes no mux. It now runs on its own port (6060), which is
  the better arrangement anyway — the metrics port is scraped by anything on the
  network and pprof must not be. Deliberately **not published** in compose.
