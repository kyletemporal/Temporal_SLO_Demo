# Temporal SLO Demo

Observability, SLOs and error budgets for a **self-hosted** Temporal Service.

Four bundles and a toolbox:

| | What it is | Use it to |
|---|---|---|
| **`demo/`** | A complete stack that runs on a laptop: Temporal, Postgres, Prometheus, Grafana, a Go app, and eight chaos scenarios | *See* what a backlog, a starved Worker and an orphaned Task Queue look like before you meet one at 2am |
| **`production/`** | Rules and dashboards for a **self-hosted** cluster, no demo app | Drop into a real self-hosted deployment |
| **`cloud/`** | Rules and dashboards for **Temporal Cloud**, built on the Cloud SLA | Monitor a Cloud Namespace and build SLOs on top of Temporal's |
| **`aws/`** | S3 Workflow-history export and EKS Worker deployment with KEDA autoscaling | Keep history past the 90-day cap; run Workers that scale on the right signal |
| **`terraform/`** | Modules for managing Temporal Cloud as code — Namespaces, team onboarding, least-privilege metrics access | Provision Namespaces and credentials repeatably instead of clicking through the UI |
| **`monitor/`** | A service that polls Visibility for workflow-**duration** SLIs, plus its rules | Detect Workflows stuck or running past budget — the failure metrics structurally cannot see |
| **`app-team/`** | The **minimum standard** for teams that build Workflows on someone else's Temporal platform | Hand to your clients; enforce with its conformance check |
| **`tools/`** | Generators for the rule files and dashboards | Regenerate after editing an SLI |

**Never load two of these bundles into one Prometheus.** `demo/` and
`production/` both record `slo:*` series with overlapping `sli` labels, so
loading both produces duplicate series and silently wrong SLO numbers. `cloud/`
is prefixed `cloudslo:*` and `app-team/` is prefixed `appslo:*`, so those can
safely share a Prometheus with the platform team's rules — which is the normal
case when application teams and the platform team scrape the same server.

**Self-hosted or Cloud is not a cosmetic difference.** Cloud metric `_count`
series are gauges holding pre-computed rates (so `rate()` is wrong), percentiles
arrive pre-calculated and cannot be re-aggregated, and there are no server
internals at all. Use `production/` or `cloud/`, not both.

---

## Security

`demo/` is a **lab**, not a deployment template. It authenticates nobody,
encrypts nothing and publishes fourteen ports — appropriate for one laptop and
one demo, and dangerous anywhere else.

**[RECOMMENDATIONS.md](RECOMMENDATIONS.md)** explains every threshold in the
repo and, more usefully, which of four kinds it is — published guidance,
measured here, structural (do not tune it away), or a placeholder you must
replace. Read it before adopting any alert.

**[SECURITY.md](SECURITY.md)** carries the full risk register, the reasoning
behind each accepted risk, a hardening checklist to work through before any of
this touches an environment, and reproducible commands for every scan behind it.
Read it before adapting `demo/`.

---

## Support and licence

**This is a community resource, not a Temporal-supported product.** It is not
covered by any Temporal support agreement or SLA, and issues with it should not
be raised as Temporal support tickets. Temporal's own community dashboards carry
the same caveat, and this repo builds on them.

Everything here is **starting material that you are expected to change**. Every
threshold is a guess until it has been baselined against two weeks of your own
data, and several alerts ship with `REPLACE_ME` markers precisely so they cannot
be adopted by accident.

Before trusting the SDK-metric alerts on your stack, run:

```bash
cd demo && make verify-sdk-labels
```

It checks that the shipped alerts match *your* SDK's actual label names and
units. This is not boilerplate caution: the published non-determinism rule uses
`error_type`, the Go SDK emits `failure_reason`, and copied verbatim that alert
never fires. Latency units differ by language too — Go and Java emit seconds,
TypeScript, Python and .NET emit milliseconds, and getting it wrong is a silent
1000x error.

Licensed under the [MIT Licence](LICENSE).

## Try it in one command

```bash
cd demo
./deploy.sh --clean
```

Checks prerequisites and ports, builds, waits for every service to be genuinely
ready, seeds traffic, and runs 37 validation checks. Takes a few minutes on
first build (Go modules + images). Then:

| | |
|---|---|
| Grafana | <http://localhost:3000> — anonymous viewer, `admin`/`admin` to edit |
| Prometheus | <http://localhost:9090> |
| Temporal UI | <http://localhost:8080> |

Dashboards: **Overview** (the home page), **Full Overview (self-hosted)**,
**Golden Signals (RED + Saturation)**, **SLO Board — Error Budgets**, and the
original **Service & Worker Overview**.

**Full Overview** is a row-for-row rebuild of Grafana Cloud's Temporal
dashboard for a self-hosted cluster. A direct port does not work — wrong
schema, `temporal_cloud_v1_*` metrics that do not exist, gauges where
self-hosted has counters, and three different label spellings for "task queue".
What was translated, replaced and dropped is in
[`docs/CLOUD-TO-SELFHOSTED.md`](docs/CLOUD-TO-SELFHOSTED.md).

```bash
make validate       # re-run all 37 checks any time
make chaos-slots    # watch an error budget drain
make chaos-stuck    # the one where nothing moves — and that IS the finding
make chaos-stuck-release   # required cleanup
make help           # everything else
```

Full walkthrough and troubleshooting: [`demo/SETUP.md`](demo/SETUP.md).

---

## Why this exists

Temporal is durable, so it does not drop work — it *queues* it. Which means a
Temporal outage does not look like an outage. It looks like every dashboard is
green and nothing is happening.

Every correction in this repo came from running the stack and disbelieving the
result. A few that changed the numbers:

- **Long-polls poison latency metrics.** `PollWorkflowTaskQueue` and
  `PollActivityTaskQueue` block up to 60s *by design* and are the highest-volume
  Frontend operations. Included: 95.9% of requests "under 500ms". Excluded:
  100%. The shipped `TemporalFrontendLatencyHigh` alert fired **forever** on a
  healthy idle cluster because of this.
- **`absent(temporal_worker_task_slots_available)` never fires.** Temporal's own
  internal Workers keep emitting that metric under `namespace="temporal_system"`,
  so the vector is never empty. 22 server-emitted series survive killing the
  entire application fleet. Absence alerts must be scoped to your namespace and
  Task Queue.
- **Saturation produces timeouts, not failures.** Under a backlog storm,
  `workflow_failed` sat at 0.02/s while `workflow_timeout` hit 24.6/s. An SLI
  watching only failures reported ~100% healthy while three quarters of all work
  expired.
- **Client errors are not your errors.** Matching emits a steady ~0.39/s of
  `serviceerror_Canceled` at idle. Counting it took availability to 98.77% and
  blew a 99.9% budget many times over while nothing was wrong.

Docs: [`demo/docs/SLO-GUIDE.md`](demo/docs/SLO-GUIDE.md) ·
[`demo/docs/CHAOS-RUNBOOK.md`](demo/docs/CHAOS-RUNBOOK.md) ·
[`demo/docs/NEXUS.md`](demo/docs/NEXUS.md) ·
[`production/docs/SERVERLESS-WORKERS.md`](production/docs/SERVERLESS-WORKERS.md)

---

## Namespaces and Nexus

The demo cluster ships with Nexus configured — cross-Namespace calls through a
named Endpoint, so two teams integrate without sharing a Namespace.

```bash
cd demo
make nexus-doctor                  # verify the setup end to end
make ns-create NAME=payments RETENTION=168h
make nexus-create EP=payments-api NS=payments TQ=billing
```

The trap it exists to catch: **registering an Endpoint succeeds even when the
callback configuration is missing entirely**, and the failure only appears at
the first real Operation invocation. `make nexus-doctor` checks what
registration does not. Details, including the 1.30 config change and what a
stock `auto-setup` already does for you, in [`demo/docs/NEXUS.md`](demo/docs/NEXUS.md).

---

## Serverless Workers

Temporal supports running Workers on AWS Lambda (Public Preview) and GCP Cloud
Run (Pre-release). Temporal starts the Worker when Tasks arrive, via a server
component called the Worker Controller Instance — there is no idle polling and
no fleet to provision.

**Self-hosting it requires Temporal Service v1.31.0 or later.** The demo here
pins **1.27.4**, so it cannot run Serverless Workers without a server upgrade.

Ephemeral Workers also invalidate specific rules in this repo — `absence` alerts
page constantly when scale-to-zero is the normal state, `task_delivery` becomes
unreachable once cold start is in the path, and Prometheus cannot scrape a
function that is not running. What to change is in
[`production/docs/SERVERLESS-WORKERS.md`](production/docs/SERVERLESS-WORKERS.md).

---

## Deploying the production bundle

`production/` has no demo app and no `auto-setup`. 46 SLO rules + 9 operational
alerts (a 10th ships commented out — see below) + 2 dashboards.

```bash
cp production/prometheus/*.yml /etc/prometheus/
promtool check rules /etc/prometheus/*.yml
```

Three things to change first — `REPLACE_ME` markers in `alerts.yml` and
`prometheus.example.yml`, and **the objectives**, which are placeholders. Run
recording-only for two weeks and set objectives you can actually meet; an SLO
you miss at baseline teaches the team to ignore the board.

See [`production/README.md`](production/README.md).

---

## Regenerating

`demo/prometheus/slo-rules.yml`, both SLO dashboards and the golden-signals
dashboard are **generated**. Edit the SLI list in the generator, not the output,
or they drift apart.

```bash
python3 tools/generate_slo_rules.py            # demo rules
python3 tools/generate_production_rules.py     # self-hosted production rules
python3 tools/generate_cloud_rules.py          # Temporal Cloud rules
python3 tools/generate_slo_board.py            # SLO board
python3 tools/generate_golden_signals.py       # golden signals (self-hosted)
python3 tools/generate_cloud_golden_signals.py # golden signals (Cloud)
python3 tools/generate_app_team_rules.py       # app-team minimum SLOs
python3 tools/generate_app_team_dashboard.py   # app-team dashboard
```

---

## Status

Verified end to end on macOS + Docker Desktop against Temporal Server 1.27.4,
Go SDK 1.47, Prometheus 3.1, Grafana 11.5, k6 0.55.

**This is a teaching rig, not a production template.** Single-node Postgres, no
backups, no TLS, no auth, anonymous Grafana, `auto-setup`, and Prometheus with
no durable volume. Use the Helm chart for anything real.

Testing it? [`TESTING.md`](TESTING.md) lists what is verified, what is not, and
where the known gaps are.
