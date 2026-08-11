# Temporal Self-Hosted Observability Demo

A single-command stack that stands up a self-hosted Temporal Service, a demo
application, Prometheus, and Grafana — plus load and chaos scenarios that drive
the dashboard into every failure mode the accompanying guide describes.

Purpose: let a customer *see* what a backlog, a starved Worker, and an orphaned
Task Queue actually look like, before they are looking at one in production at
2am.

## Quick start

```bash
./deploy.sh
```

One command: checks prerequisites and ports, builds, starts, waits for every
service to be genuinely ready, seeds traffic, and runs the full validation
suite. Add `--clean` to wipe and start from scratch. Safe to re-run.

Or drive it by hand:

```bash
make up          # build and start everything (first build takes a few minutes)
make smoke       # run one order through the system — also seeds the counters
make verify      # confirm metrics are flowing END TO END
make validate    # full check: containers, targets, rules, every panel, SLOs
make baseline    # 10 minutes of healthy traffic — do this before any chaos
```

`smoke` before `verify` is deliberate. Several Temporal metrics are counters
that do not exist until traffic has produced one, so verifying a fresh, idle
stack reports a missing metric that is merely unborn.

New here, or handing this to a customer? **[`SETUP.md`](SETUP.md)** is the
step-by-step version: prerequisites, expected output at each step, and a
troubleshooting section covering every failure actually hit while validating
this stack.

| Service | URL | Credentials |
|---|---|---|
| Grafana | http://localhost:3000 | anonymous viewer; `admin` / `admin` to edit |
| Prometheus | http://localhost:9090 | — |
| Temporal UI | http://localhost:8080 | — |
| Demo API | http://localhost:8081 | — |

Then work through `docs/CHAOS-RUNBOOK.md`.

## Do not skip `make verify`

A config that *looks* right is not a working pipeline. `make verify` checks
what actually matters:

- every Prometheus target is UP
- `service_requests` exists (cluster metrics are flowing)
- `temporal_worker_task_slots_available` exists (SDK metrics are flowing)
- `poll_success_sync` exists (the Sync Match Rate panel will render)

That last one is the fragile one — see *Known sharp edges* below.

## What is in the box

```
deploy.sh              One-shot deploy + validate
SETUP.md               Step-by-step setup + troubleshooting (start here)
docs/SLO-GUIDE.md      Error budgets and burn-rate alerting for every service role
docker-compose.yml     Temporal (auto-setup + Postgres), Prometheus, Grafana, app
app/                   Go demo application (API + Worker, one binary, MODE switch)
prometheus/            Scrape config, alerting rules, SLO + error budget rules
grafana/               Datasource + dashboard provisioning
  dashboards/custom/     The minimal self-hosted overview dashboard
  dashboards/slo/        SLO board: error budgets and burn rates
  dashboards/community/  Populated by `make dashboards` (see below)
k6/                    Five load/chaos scenarios
scripts/               Community dashboard importer, worker blackout chaos
docs/CHAOS-RUNBOOK.md  What each scenario proves and how to read it
```

### Community dashboards

```bash
make dashboards
```

Clones [temporalio/dashboards](https://github.com/temporalio/dashboards) and
provisions the `server/` and `sdk/` dashboards into a **Community** folder in
Grafana.

Two things this handles that a plain `git clone` does not:

1. **Import scaffolding.** Those dashboards are published for manual import
   through the Grafana UI and carry an `__inputs` block with `${DS_PROMETHEUS}`
   placeholders that the wizard fills in interactively. File provisioning has no
   wizard, so the placeholder never resolves and every panel reports
   *"Datasource ${DS_PROMETHEUS} was not found"*. `scripts/normalize_dashboard.py`
   strips the scaffolding and repoints every datasource reference at the
   provisioned Prometheus. Dashboard-local template variables such as
   `${datasource}` are deliberately left alone.
2. **Filename drift.** The provider globs a directory rather than listing
   filenames, so upstream renames do not break the stack.

`cloud/` is excluded on purpose — those dashboards query `temporal_cloud_v1_*`
metrics, which do not exist self-hosted. Provisioning them would add a folder of
permanently empty panels, and a dashboard that is empty *by design* teaches
people to ignore panels that are empty *by accident*. `misc/` is excluded because
upstream flags it as untested.

The upstream README is explicit that these are community dashboards, not
production-ready and not tested against every server version. Treat them as a
reference to learn from, not a deliverable.

## Demo application

One Go binary, two modes. Go was chosen because it emits histogram durations in
**seconds**, matching the shipped dashboard and alert thresholds. TypeScript,
Python, and .NET emit **milliseconds** — if you re-point this at a customer app
in one of those, see §2.1 of the guide before trusting any latency panel.

Chaos levers travel in the request body:

```bash
curl -X POST localhost:8081/orders -H 'Content-Type: application/json' -d '{
  "orderId": "demo-1",
  "failureRate": 0.5,        # per-attempt chance ChargePayment fails
  "activityDelayMs": 500,    # artificial latency; holds an Activity slot open
  "maxAttempts": 1,          # 1 = Activity failures become Workflow failures
  "taskQueue": "orders",     # set to a queue nobody polls to orphan the work
  "wait": false              # true blocks until the Workflow completes
}'
```

Two deliberate choices in the application code, both called out in comments:

- **`ScheduleToStartTimeout` is not set.** Setting it truncates
  `temporal_activity_schedule_to_start_latency` at the timeout, so a badly
  backed-up queue reports a flat line instead of a rising one. Confirming this
  option is unset is step one of any real backlog investigation.
- **Worker concurrency defaults to 10**, against Temporal's real default of
  1000. Slot exhaustion is otherwise unreachable on a laptop, and slot
  exhaustion is half the diagnostic value of the dashboard.

## Known sharp edges

Flagged rather than hidden, because each one will bite during a live demo:

**`poll_success_sync` may not exist on your server version.** It powers the Sync
Match Rate panel. `make verify` checks for it. Rule it out first: it is a
counter, so it does not exist on an idle stack that has never run a Workflow —
run `make smoke` and re-check before concluding anything. If it is still
missing, run
`curl -s localhost:8000/metrics | grep poll_success` against the Temporal
container to find the name your build emits, and update the panel and the
`TemporalSyncMatchRateLow` alert rule to match. This is the one query in the set
that varies most across releases.

**The Temporal health check must not target `127.0.0.1`.** `auto-setup` binds
the Frontend to the container's own eth0 address, so nothing listens on
loopback. A health check against `127.0.0.1:7233` gets connection-refused
forever, the container never reports healthy, and every service gated on
`condition: service_healthy` — `api`, `worker`, `temporal-ui` — silently never
starts. The compose file uses `$(hostname -i)` instead. This one is nasty
because `docker compose ps` shows Temporal *running*; only the health status
tells you why nothing else came up.

**`auto-setup` bundles all four service roles into one container.** Metrics for
frontend, history, matching, and worker all arrive on `temporal:8000`,
distinguished by the `service_name` label. Every dashboard query already filters
on `service_name`, so nothing changes when you split them — but `prometheus.yml`
would list four targets instead of one. Do not ship `auto-setup` to production;
it exists for demos and local development.

**The Go metrics bootstrap is the file most likely to drift.** `app/metrics.go`
wires tally to Prometheus. If the build fails on dependency resolution, compare
it against the current
[samples-go metrics sample](https://github.com/temporalio/samples-go/tree/main/metrics),
which is the canonical reference.

Note that tally v4 now types `ConfigurationOptions.Registry` against **upstream**
`github.com/prometheus/client_golang`, not the m3db fork that older examples
import. Using the fork is both a type error and the source of an ambiguous
`google.golang.org/genproto` import.

**Dependencies are pinned** in `app/go.mod` and `app/go.sum`. An earlier version
of this template deliberately left them unpinned and ran `go mod tidy` at build
time, reasoning that pinned versions go stale. In practice the unpinned build
was the thing that broke — three ways at once: the SDK's minimum Go version
moved past the builder image, the m3db fork poisoned the module graph, and two
SDK symbols were renamed. A demo that will not build is worth less than a demo
on last quarter's SDK. Refresh deliberately with `make deps-refresh`, which
produces a reviewable diff.

**First build needs network access** for `go mod tidy` and the base images.

**Resource footprint** is roughly 2 CPU / 3GB RAM for the full stack. Scenario 1
at `--scale worker=5` will make a laptop work. Reduce the `target` rates in
`k6/01-backlog-storm.js` if your machine struggles — the shapes hold at lower
volume.

## Not production ready

Deliberate shortcuts: single-node Postgres with no backups, no TLS, no
authentication on Temporal, anonymous Grafana access, 6-hour Prometheus
retention, hardcoded credentials. This is a teaching rig. Use the Helm chart for
anything real.
