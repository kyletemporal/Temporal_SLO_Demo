# Setup Guide

Getting the Temporal self-hosted observability demo running on your machine.

Everything here was executed end to end on a clean machine on 2026-08-10 against
Temporal Server 1.26.2, Go SDK 1.47.0, Grafana 11.5.1, Prometheus 3.1.0, and k6
0.55.0. The commands below are the ones that were actually run, in order.

If you only read one section, read **[Step 3: Verify](#step-3-verify-do-not-skip-this)**.

---

## Before you start

| Requirement | Why | Check |
|---|---|---|
| Docker Desktop (or Engine) with Compose v2 | Runs the whole stack | `docker compose version` |
| ~4 GB free RAM allocated to Docker | Seven containers, one of them a database | Docker Desktop → Settings → Resources |
| ~3 GB free disk | Images plus the Go module cache | `df -h` |
| Network access on first build | Pulls base images and Go modules | — |
| `make`, `curl`, `python3` | Driving and verifying the stack | `make -v && python3 -V` |
| Ports free: 3000, 7233, 8000, 8080, 8081, 9090 | Bound to the host | `lsof -i :3000` |

Apple Silicon and x86 both work; images are multi-arch.

**Time to first working dashboard:** about 5 minutes, most of it the first Go
build. Later builds take seconds.

---

## Step 1: Get the files

```bash
tar -xzf temporal-observability-demo.tar.gz
cd temporal-observability-demo
```

If your extraction tool dropped the executable bit on the helper scripts, restore
it. The Makefile invokes them through `bash` so this is belt-and-braces, but
running them directly will fail without it:

```bash
chmod +x scripts/*.sh scripts/*.py
```

---

## The short version

If you would rather not read the rest of this:

```bash
./deploy.sh --clean
```

It runs every step below — prerequisite and port checks, build, start, readiness
waits, traffic seed, and the full validation suite — and tells you what broke if
anything does. The remaining sections explain what it is doing and what each
result means.

---

## Step 2: Start the stack

```bash
make up
```

This builds the Go application image and starts seven containers. The first run
pulls images and downloads Go modules, so expect a few minutes; subsequent runs
start in seconds.

Expected tail:

```
  Grafana      http://localhost:3000  (anonymous viewer, admin/admin to edit)
  Prometheus   http://localhost:9090
  Temporal UI  http://localhost:8080
  Demo API     http://localhost:8081

  Next: make verify
```

Confirm all seven are up:

```bash
docker compose ps
```

`tobs-temporal` must reach **healthy**, not just running. `api`, `worker`, and
`temporal-ui` are gated on that health check and will not start until it passes.
It typically flips healthy 40–60 seconds after `make up`.

---

## Step 3: Verify (do not skip this)

A configuration that *looks* right is not a working pipeline. The single most
common failure mode with Temporal observability is a dashboard that renders
beautifully and is quietly wired to nothing.

First push one Workflow through end to end:

```bash
make smoke
```

```json
{
    "workflowId": "order-smoke-1",
    "runId": "8194823f-e75c-4929-bd38-93ffaa76fa52",
    "taskQueue": "orders",
    "result": "completed"
}
```

`"result": "completed"` means API → Temporal → Worker → Activities all work.

Run this **before** `make verify`, not after. Several Temporal metrics are
counters that do not exist until something has happened — verifying a fresh,
idle stack reports them missing when they are merely unborn, and sends you
debugging a problem you do not have.

Then, once traffic has flowed:

```bash
make verify
```

What a healthy stack prints:

```
==> Prometheus targets
    prometheus               up       http://localhost:9090/metrics
    temporal-sdk-api         up       http://api:8077/metrics
    temporal-sdk-worker      up       http://172.19.0.7:8077/metrics
    temporal-service         up       http://temporal:8000/metrics

==> Cluster metrics present (expect a non-empty result)
    service_requests: 33

==> SDK metrics present (expect a non-empty result)
    task_slots_available: 25

==> Sync match rate metric present (verifies the panel will render)
    poll_success_sync: 3
```

Four checks, four distinct failure meanings:

| Check | If it fails |
|---|---|
| All four targets `up` | Something is not running or not reachable — start here, the rest is noise until this is clean |
| `service_requests` | Cluster metrics are dark. `PROMETHEUS_ENDPOINT` is not set on the `temporal` service |
| `temporal_worker_task_slots_available` | SDK metrics are dark. The application is not exporting — no Prometheus setting fixes this, it is application code |
| `poll_success_sync` | The Sync Match Rate panel will be empty. Almost always means no traffic yet; otherwise the name varies by server version — see troubleshooting |

The numbers will differ from the sample; only empty-vs-present matters.

If `temporal-sdk-worker` is missing from the target list entirely, wait ten
seconds and re-run. Worker replicas are discovered through Compose DNS on a 10
second refresh, so a `verify` run fired immediately after `make up` can list
only three targets. It is absent, not down — it appears on the next refresh.

---

## Step 4: Open the dashboard

<http://localhost:3000> → **Dashboards** → **custom** →
*Temporal Self-Hosted — Service & Worker Overview*

Anonymous access is read-only; log in with `admin` / `admin` to edit.

Before generating load, four panels are empty **and should be**:

- *Tasks With No Poller* — its title says "expect zero"
- The failed / timeout / cancel series of *Workflow Outcomes* — nothing has
  failed yet

Every other panel should show data within ~30 seconds of `make smoke`. If a
panel that should have data is empty, that is a real problem — see
troubleshooting.

### The SLO board

Grafana → **slo** folder → *Temporal SLO Board — Error Budgets*

Nine SLIs covering every Temporal service role, each with an objective, an error
budget, and a burn rate. At baseline everything should show positive budget
remaining; `worker_task_delivery` and `matching_latency` sit lower than the rest
because they are the two the lab's deliberately-small Worker actually
constrains.

Two things on that board are correct even though they look wrong:

- **A `NaN` row.** The `server` role often has no persistence traffic in a quiet
  window, and you cannot compute a success ratio over zero events.
- **`worker_task_delivery` at a 90% objective** rather than 99%. That is a
  measurement of what `MAX_CONCURRENT_ACTIVITIES=10` can actually deliver.

Full reasoning, including three SLI definitions that were measurably wrong
before correction, is in [`docs/SLO-GUIDE.md`](docs/SLO-GUIDE.md).

### Optional: community dashboards

```bash
make dashboards
```

Clones [temporalio/dashboards](https://github.com/temporalio/dashboards) and
provisions five dashboards into a **community** folder. These are published for
manual UI import and carry `${DS_PROMETHEUS}` placeholders that file
provisioning cannot resolve; `scripts/normalize_dashboard.py` rewrites them to
point at the provisioned Prometheus.

They are community dashboards — not Temporal-supported, not production-ready,
not tested against every server version. Some panels will be empty because your
build does not emit those metrics. Treat them as reference, not deliverable.

---

## Step 5: Establish a baseline

Do this before any chaos. You cannot tune an alert you have never baselined, and
scenarios 1–5 are uninterpretable without a reference shape.

```bash
make baseline        # 10 minutes of healthy traffic at 20 orders/sec
```

For a quick check instead of the full run:

```bash
docker compose --profile tools run --rm -e DURATION=90s k6 run /scripts/00-baseline.js
```

A healthy baseline ends with `checks: 100.00%` and `http_req_failed: 0.00%`.

Write these four numbers down — they are your reference for everything after:

- Frontend P95 latency per operation
- Persistence P95 latency per operation
- Sync match rate (should sit at or near 100%)
- Schedule-to-start P99 (should sit near zero)

---

## Step 6: Run the chaos scenarios

Work through [`docs/CHAOS-RUNBOOK.md`](docs/CHAOS-RUNBOOK.md). Short version:

Watch the **SLO board** alongside them — chaos scenarios are how you see an
error budget actually drain. Verified: a 4-minute `make chaos-slots` run took
`worker_task_delivery` from +44% budget remaining to **−13%** and put
`SLOErrorBudgetExhausted` into `pending`, while every cluster SLI stayed at
100% — the budget correctly blamed the Worker fleet, not Temporal.

| Command | Proves |
|---|---|
| `make chaos-backlog` | Not enough Worker capacity |
| `make chaos-failures` | Activity failures ≠ Workflow failures |
| `make chaos-orphan` | Task Queue name mismatch — the one signal with no false-positive mode |
| `make chaos-slots` | Concurrency set too low (looks identical to backlog; the fix is opposite) |
| `make chaos-blackout` | Absent Workers emit **no** metrics — panels go blank, not red |

Every scenario except `chaos-blackout` accepts a shorter run:

```bash
docker compose --profile tools run --rm -e DURATION=60s k6 run /scripts/03-orphan-queue.js
```

**Reset between scenarios**, or you will read the tail of the previous one:

```bash
make scale-reset
docker compose restart worker
```

Wait for schedule-to-start to return to baseline before starting the next one.
For a completely clean slate: `make down && make up`.

---

## Troubleshooting

### `make up` fails: "toolchain upgrade needed"

```
go: go.temporal.io/sdk@v1.47.0 requires go >= 1.25.4 (running go 1.23.12; GOTOOLCHAIN=local)
```

The Temporal SDK raised its minimum Go version above the builder image. Bump the
tag at the top of `app/Dockerfile` (`FROM golang:1.25-alpine`) to meet the
version in the error and rerun. The alpine images set `GOTOOLCHAIN=local`, which
is why this fails loudly instead of silently downloading a newer toolchain.

### `make up` fails: "ambiguous import: google.golang.org/genproto/..."

Your `app/go.mod` is importing the **m3db fork** of
`prometheus_client_golang`, which pulls a 2021 `google.golang.org/genproto` that
collides with the modern `genproto/googleapis/rpc` submodule. Current tally v4
uses upstream `github.com/prometheus/client_golang`. The shipped `app/metrics.go`
already imports the correct one — this only bites if you have pasted in an older
example.

### `tobs-temporal` never becomes healthy, and `api`/`worker` never start

```
dependency failed to start: container tobs-temporal is unhealthy
```

Check what the health check actually reports:

```bash
docker inspect tobs-temporal --format '{{json .State.Health}}' | python3 -m json.tool
```

If it says `dial tcp 127.0.0.1:7233: connect: connection refused`, the health
check is pointed at loopback. `auto-setup` binds the Frontend to the
**container's own IP**, not `0.0.0.0`, so nothing listens on `127.0.0.1`. The
shipped `docker-compose.yml` uses `$(hostname -i)` for exactly this reason.
Confirm what it is bound to:

```bash
docker exec tobs-temporal netstat -tlnp | grep 7233
```

### `make verify` says `poll_success_sync` is not yet emitted

**Check this first:** it is a counter, and counters do not exist before the
event they count. On a stack that has never run a Workflow it is legitimately
absent. Run `make smoke`, wait ~15 seconds for the next scrape, and re-run
`make verify`. This resolves it the overwhelming majority of the time.

If it is still missing *after* traffic, then the name genuinely differs on your
server version — it varies more than any other metric in this set. Find what
your build emits:

```bash
docker exec tobs-temporal wget -qO- localhost:8000/metrics | grep poll_success
```

Then update two places to match: the *Sync Match Rate* panel in
`grafana/dashboards/custom/temporal-self-hosted-overview.json`, and the
`TemporalSyncMatchRateLow` rule in `prometheus/alerts.yml`.

### A k6 scenario exits immediately with a threshold error

```
level=error msg="unable to validate threshold expressions; reason: parsing metric name failed"
```

k6 0.55 rejects an empty tag expression such as `'checks{}'`. Write plain
`checks:` instead. All shipped scenarios are already correct; this bites when
you copy a threshold from older k6 examples.

### The Worker Fleet Health panels are empty

Two very different causes, and telling them apart is the point of the demo:

- **No Workers running** → the series does not exist. Blank, not zero. Check
  `docker compose ps`. A threshold alert can never catch this; only
  `absent(temporal_worker_task_slots_available)` will.
- **Workers running but not exporting** → `make verify` shows the
  `temporal-sdk-worker` target as down. The metrics exporter is not wired up in
  application code.

### Grafana logs errors about `provisioning/plugins` and `provisioning/alerting`

```
level=error msg="Failed to read plugin provisioning files from directory"
```

Harmless. Grafana probes for optional provisioning subdirectories that this
stack does not use. Nothing is broken.

### Prometheus logs `could not resolve "worker"`

Expected whenever the Worker fleet is stopped — including during
`make chaos-blackout`, where it is the point. Prometheus discovers Worker
replicas through Compose DNS, and there is nothing to resolve while they are
down. It recovers within ~10 seconds of Workers returning.

### Port already in use

Another service holds one of the ports. Find it with `lsof -i :3000` (substitute
the port), then either stop it or change the host side of the mapping in
`docker-compose.yml` — `"3001:3000"` moves Grafana without touching anything
inside the stack.

### Starting completely over

```bash
make down          # stops everything and removes volumes
make up
```

---

## Maintenance

Go dependencies are **pinned** in `app/go.mod` and `app/go.sum`. To refresh them
deliberately:

```bash
make deps-refresh
```

This re-resolves against current upstream and rebuilds. It can break the build —
that is why it is a deliberate action with a reviewable diff rather than
something that happens automatically on a customer's laptop before a demo.

---

## Reference

| Service | URL | Credentials |
|---|---|---|
| Grafana | <http://localhost:3000> | anonymous viewer; `admin` / `admin` to edit |
| Prometheus | <http://localhost:9090> | — |
| Temporal UI | <http://localhost:8080> | — |
| Demo API | <http://localhost:8081> | — |

```bash
make help          # every available target
make logs          # tail api + worker
make verify        # re-check the pipeline any time
make down          # stop everything, remove volumes
```

Driving the app directly:

```bash
curl -X POST localhost:8081/orders -H 'Content-Type: application/json' -d '{
  "orderId": "demo-1",
  "failureRate": 0.5,
  "activityDelayMs": 500,
  "maxAttempts": 1,
  "taskQueue": "orders",
  "wait": false
}'
```

| Field | Effect |
|---|---|
| `failureRate` | Per-attempt chance `ChargePayment` fails (0–1) |
| `activityDelayMs` | Artificial latency; holds an Activity slot open |
| `maxAttempts` | `1` turns Activity failures into Workflow failures |
| `taskQueue` | Point at a queue nobody polls to orphan the work |
| `wait` | `true` blocks until the Workflow completes |

---

## This is a teaching rig, not a production template

Deliberate shortcuts: single-node Postgres with no backups, no TLS, no
authentication on Temporal, anonymous Grafana access, 6-hour Prometheus
retention, hardcoded credentials, and `auto-setup` running all four Temporal
service roles in one container. Use the Helm chart for anything real.
</content>
</invoke>
