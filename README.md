# Temporal SLO Demo

Observability, SLOs and error budgets for a **self-hosted** Temporal Service.

Two things live here:

| | What it is | Use it to |
|---|---|---|
| **`demo/`** | A complete stack that runs on a laptop: Temporal, Postgres, Prometheus, Grafana, a Go app, and five chaos scenarios | *See* what a backlog, a starved Worker and an orphaned Task Queue look like before you meet one at 2am |
| **`production/`** | Just the rules and dashboards, no demo app | Drop into a real cluster |
| **`tools/`** | Generators for the rule files and dashboards | Regenerate after editing an SLI |

---

## Try it in one command

```bash
cd demo
./deploy.sh --clean
```

Checks prerequisites and ports, builds, waits for every service to be genuinely
ready, seeds traffic, and runs 26 validation checks. Takes a few minutes on
first build (Go modules + images). Then:

| | |
|---|---|
| Grafana | <http://localhost:3000> — anonymous viewer, `admin`/`admin` to edit |
| Prometheus | <http://localhost:9090> |
| Temporal UI | <http://localhost:8080> |

Dashboards: **Golden Signals (RED + Saturation)**, **SLO Board — Error Budgets**,
and the original **Service & Worker Overview**.

```bash
make validate       # re-run all 26 checks any time
make chaos-slots    # watch an error budget drain
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
[`production/docs/SERVERLESS-WORKERS.md`](production/docs/SERVERLESS-WORKERS.md)

---

## Deploying the production bundle

`production/` has no demo app and no `auto-setup`. 46 SLO rules + 10
operational alerts + 2 dashboards.

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
python3 tools/generate_slo_rules.py           # demo rules
python3 tools/generate_production_rules.py    # production rules
python3 tools/generate_slo_board.py           # SLO board
python3 tools/generate_golden_signals.py      # golden signals
```

---

## Status

Verified end to end on macOS + Docker Desktop against Temporal Server 1.26.2,
Go SDK 1.47, Prometheus 3.1, Grafana 11.5, k6 0.55.

**This is a teaching rig, not a production template.** Single-node Postgres, no
backups, no TLS, no auth, anonymous Grafana, `auto-setup`, and Prometheus with
no durable volume. Use the Helm chart for anything real.

Testing it? [`TESTING.md`](TESTING.md) lists what is verified, what is not, and
where the known gaps are.
