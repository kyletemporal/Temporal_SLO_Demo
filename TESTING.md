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
