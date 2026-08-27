# CLAUDE.md

Guidance for working in this repo.

## What this is

Observability for Temporal — metrics, logs, traces, profiles, duration SLOs and
infrastructure-as-code — organised as independent bundles rather than one system.

| Directory | Scope | Prefix |
|---|---|---|
| `demo/` | Runnable laptop stack: Temporal, Postgres, Prometheus, Grafana, Loki, Tempo, Pyroscope, a Go app, 8 chaos scenarios | `slo:` |
| `production/` | Self-hosted cluster rules + dashboards, no demo app | `slo:` |
| `cloud/` | Temporal Cloud, built on the Cloud SLA | `cloudslo:` |
| `app-team/` | Minimum standard for teams building on someone else's platform | `appslo:` |
| `monitor/` | Go service polling Visibility for workflow-**duration** SLIs | `temporal_slo_*` |
| `terraform/` | Temporal Cloud IaC — modules, examples, design patterns | — |
| `tools/` | Generators. **Dashboards and some rules are generated** | — |

**Never load `demo/` and `production/` into one Prometheus.** Both record `slo:*`
with overlapping `sli` labels; loading both silently produces wrong SLO numbers.
`cloud/` and `app-team/` use distinct prefixes and can coexist.

## Working rules

**Edit the generator, not the JSON.** Dashboards come from `tools/generate_*.py`
and `terraform/`-adjacent rule files from `tools/generate_visibility_rules.py`.
Hand-editing generated output is silently reverted on the next run. After
changing a generator, regenerate and check for gridPos overlaps.

**`tools/add_poll_outcome_panels.py` runs AFTER `generate_golden_signals.py`,
and the order is load-bearing.** The generator rewrites the golden-signals JSON
from scratch, so re-running it silently drops row P. Always:

```bash
python3 tools/generate_golden_signals.py demo/grafana/dashboards/slo/temporal-golden-signals.json
python3 tools/add_poll_outcome_panels.py
```

The post-processor is idempotent (marker-based, `_pollOutcomeOwned`) and exits
rather than overwrite on a panel-ID collision. It is demo-only on purpose —
`generate_golden_signals.py` writes both `demo/` and `production/`, and these
panels have no business on a production board.

**Verify against the running stack, not the docs.** This repo exists partly
because published guidance has been wrong in ways that produce *silent* failures.
Everything below was found by running it.

**`terraform validate` and `promtool check rules` prove syntax, not behaviour.**
A rule that parses can still return empty. Query live data before believing it.

## Validation

```bash
cd demo && make validate            # 37 checks: containers, targets, rules, panels, SLOs, logs, trace links
cd demo && make verify-sdk-labels   # confirms alerts match YOUR SDK's labels and units
cd monitor && go test ./...
cd terraform && terraform fmt -check -recursive
for d in terraform/examples/*/ terraform/patterns/*/; do (cd $d && terraform validate); done
python3 tools/generate_visibility_rules.py --check   # drift check
```

## Traps this repo has already hit

Each of these shipped as a silent failure — an alert that looks correct in review
and never fires, or a number that looks like data and is not.

**Alerts and metrics**

- **NDE label is `failure_reason`, not `error_type`.** The published rule matches
  nothing on the Go SDK via tally. Verified by forcing a real NDE. Other SDKs may
  differ — `make verify-sdk-labels`.
- **SDK latency units differ by language.** Go/Java emit **seconds** with a
  `_seconds` suffix; TS/Python/.NET emit **milliseconds** without it. 200ms is
  `0.2` or `200`. A silent 1000× error.
- **Never `rate()`/`increase()`/`histogram_quantile()` on `temporal_cloud_v1_*`.**
  They are pre-computed per-second gauges. `sum()` only.
- **`absent(temporal_worker_task_slots_available)` never fires** on self-hosted —
  the server's own internal workers keep emitting it under
  `namespace="temporal_system"`. Scope absence alerts to your namespace and queue.
- **Ratio alerts need an `or ... * 0` guard.** Without it, a total outage produces
  no right-hand series, the expression returns nothing, and the alert stays
  silent exactly when it matters.
- **Idle queues make ratio alerts fire.** Sync-match and poll-success rules need a
  traffic floor.

- **Two queries in one panel sharing a `refId` is not an error.** Grafana renders
  one and silently drops the rest, so the panel looks half-populated with no
  message anywhere. Shipped once; `validate.sh` now checks every dashboard.
- **`poll_timeouts` is NOT an async-match counter.** Temporal exposes no
  async-match metric — async match is `poll_success - poll_success_sync`.
  Reading `poll_timeouts` as async match inverts the conclusion: an
  over-provisioned fleet reads as a starved one.
- **Poll success rate cannot page anyone alone.** A starved fleet and a flooded
  fleet both push it down and need *opposite* responses. It needs a second,
  independent condition (schedule-to-start). Reproduce with
  `make chaos-poller-flood`.
- **Scope poll ratios to your own Task Queue.** Temporal's `temporal_sys_*`
  queues long-poll constantly and match nothing — measured, `poll_timeouts`
  spans 68 series across 6 namespaces while `poll_success` spans 26 across 2.
  Unscoped, the ratio sits low permanently and barely moves during a real event.

**Grafana and tooling**

- **Anonymous Viewers cannot reach Explore.** `datasources:explore` is granted to
  Editor and above; a Viewer opening `/explore` is *silently redirected to the
  home page*. That kills every trace link at once. `GF_USERS_VIEWERS_CAN_EDIT`.
- **`docker compose restart` does NOT apply compose-file changes**, and
  `--scale N` down leaves the surplus containers in place for a later command to
  restart. Use `up -d --force-recreate`, and `rm -sf` before re-creating.
- **`curl | grep -q` under `set -o pipefail` reports a false failure.** grep
  exits on first match, curl takes SIGPIPE, and the pipeline fails despite the
  match succeeding. Capture to a variable and match against that.

**PromQL**

- **`count()` of an empty vector returns no data, not `0`.** A "things in breach"
  panel then shows *No data* on a healthy system, making the all-clear and a
  broken query identical. End with `or vector(0)`.

- **Binary operators need identical label sets.** `over_budget_executions` carries
  a `bucket` label the closed gauges do not, so the SLI returned **empty** — not a
  wrong number — until `sum without(bucket)`. An empty SLI is reported by nothing.
- **A matching modifier needs vectors on both sides.** `1 - on(...) group_left()
  vec` is a parse error; wrap the subtraction instead.

**Infrastructure**

- **`docker compose run` containers are not scrape targets.** Prometheus finds
  workers by `dns_sd` on the service name. `chaos-nde` was unobservable for this
  reason — run injection through the service, not a `run` container.
- **`grafana_notification_policy` manages the ENTIRE tree and overwrites it.** A
  per-team copy makes each team's apply erase every other team's routing. Route
  per-rule via `notification_settings.contact_point` instead.
- **`temporalcloud_metrics_endpoint` provisions the DEPRECATED PromQL endpoint**
  (mTLS, disabled 2026-10-05). The current path is a `metricsread` service account
  + API key for OpenMetrics.
- **Provider docs lag the schema by two major versions.** Read
  `terraform providers schema -json`. `certificate_filters`/`codec_server` are
  attributes in v1.7.0, not blocks — documented `dynamic` syntax fails.
- **`resource.Merge` with an explicit `semconv.SchemaURL` fails** when app and SDK
  semconv versions differ. Use `resource.NewSchemaless`.

## Design commitments

**No `workflow_id` or `run_id` as a label.** Anywhere — metrics, Loki, Pyroscope.
Unbounded cardinality. Logs carry it in the **line**, extracted at query time;
that is what makes log→trace correlation work without trace IDs in logs.

**A failed poll never publishes a zero.** `monitor/` leaves the previous gauge
value in place, because `0` reads as "nothing is over budget" during exactly the
outage where that is least likely to be true. The cost is that a frozen gauge
looks healthy, so `MonitorPollsStale` alerts on freshness. **Neither half is safe
without the other.**

**Absent, not zero, for unavailable signals.** `stuck_executions` is not published
when `TemporalReportedProblems` is missing, because a `0` is indistinguishable
from a clean bill of health.

**An open execution past budget is already a violation.** It sits in the SLI
denominator immediately, so terminating a stuck workflow cannot improve the
number. Verified: 5 executions moved between denominator terms, compliance held
at `0.793336` exactly.

**Observability fails open.** Tracing and profiling never block a Worker from
polling.

## Style

Comments explain **why**, especially where the obvious form is wrong — most
comments here exist because something failed in a way that looked fine. Keep the
measurement in the comment (`verified: 644 phantom errors → 0`), because that is
what stops someone "simplifying" it back.
