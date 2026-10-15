# Worker tuning lab

A live UI for changing Temporal Worker options and watching what the SDK and
server do in response: pollers, slots, sticky cache, CPU, the Activity's
resource profile, and the shape of the workload.

```bash
cd demo
make up      # the demo stack, if it is not running
make lab     # build and start the lab → http://localhost:8095
make lab-down
```

The lab starts idle. Pick a preset, or press **Start load**, on either lane.

## What it is, and what it is not

It was inspired by
[temporal-worker-tuning-simulator](https://github.com/cvilenio/temporal-worker-tuning-simulator),
a browser simulator that teaches the same ideas. The presets use the same
bottleneck names, so you can predict a result there and then check it here.
None of that project's code is used here; it has no license.

The difference is that **nothing here is modelled**. There are no simulated
tasks or modelled queues, and no CPU model:
- Workflows run on the demo's real Temporal OSS server.
- Each lane is a real Worker: Go SDK 1.47.0 and Python SDK 1.34.0, on Task
  Queues `lab-go` and `lab-python`.
- Every slot, poller, latency and cache figure is the SDK's own metric,
  captured as it's emitted.
- CPU is the Worker process's measured CPU time.
- The "io" dependency is a real HTTP service with a real concurrency limit.

The trade-off: a simulation can show any situation instantly, while this lab
can only show what actually happens, at the speed it happens. In return it
shows things a model leaves out. Several of the findings below are exactly
those.

## Lanes and controls

The same Workflow runs on both lanes: *phases × parallel Activities*.

Each control applies in one of four ways, and the UI labels each control with
its mode:

| Mode | Controls | How it applies |
|---|---|---|
| **live** | load rate, Activity duration/jitter/profile, dependency concurrency, CPUs (Go) | Immediately, including to Activities already running |
| **next** | phases, Activities per phase | Only Workflows started after the change. A running Workflow's code path must stay deterministic. |
| **swap** | pollers, slots; sticky cache size on Python | The Worker is replaced in place. The new one starts polling before the old one drains, so the queue never loses its pollers. |
| **restart** | sticky cache size on Go | The Go SDK sizes its sticky cache once per process. The Worker exits and Compose restarts it (about 2 s, visible in the UI). |

Activity profiles:
- **sleep:** pure latency.
- **cpu:** a fixed amount of SHA-256 work, calibrated to the duration on one idle core. It takes longer when the CPU is contended.
- **io:** a real HTTP call to a dependency that admits only *N* calls at a time.

## Where the numbers come from

| Shown | Source |
|---|---|
| Slots, pollers, schedule-to-start, execution time, replay latency, cache hits, completions | Each Worker's SDK metrics, captured in-process. Go: a metrics-handler wrapper (Prometheus still gets everything). Python: the SDK's `MetricBuffer`. |
| CPU | Process CPU time (`getrusage`) against `GOMAXPROCS` for Go, and against the GIL's **one core** for Python |
| Queue depth | Little's law: dispatch rate × mean schedule-to-start, both from the SDK and with no lag |
| Server gauge, dispatch rate, sync match | The server's own `approximate_backlog_count`, `poll_success` and `poll_success_sync` in Prometheus, summed across partitions (10 s scrape) |
| Pollers seen by the server | `DescribeTaskQueue` |
| Poll latency | The SDK's own long-poll RPC latency (`long_request_latency`, by operation) |
| Server persistence p95, task-completion RPC p95 | The server's `persistence_latency` and frontend `service_latency` for `Respond*TaskCompleted`, via Prometheus |

The Go lab Worker is also scraped by Prometheus, so the existing Grafana
boards show it. Note that the label is `task_queue="lab_go"`: the Prometheus
naming rewrites the dash. The Python Worker isn't scraped, because the Python
SDK exports metrics to one destination only, and here that is the lab.

The diagnosis line is a heuristic, and is labelled as one. Its inputs are
measurements. `lab_test.go` pins it to the measured cases below.

## What the presets actually showed

All on Docker Desktop on one laptop, demo stack running, one Worker per lane,
after 60 s of each preset from an empty queue.

| Preset | Measured | Diagnosis |
|---|---|---|
| Baseline (Go) | 4 Workflows/s → 24 Activities/s; Activity schedule-to-start p95 8 ms; sync match 99% | Keeping up |
| Slot constrained (Go) | Activity slots 1/1; about 162 queued; schedule-to-start p95 37.7 s; sync match 0%; CPU 0.02 of 2 | Slot-bound |
| Poller constrained (Go) | 1 poller kept up to about 200 Activities/s; past that, about 35 queued with 33 of 50 slots free | Poller-bound |
| CPU constrained (Go) | CPU 1.00 of 1; Activity slots only 7/20 busy; Workflow Task schedule-to-start 28 s | CPU-bound (**not** poller-bound) |
| I/O constrained (Go) | Activity slots 20/20; 17 calls queued at the dependency; CPU 0.03 | Downstream-bound |
| Cache constrained (Go) | Sticky cache hit 0%; replay p95 0.4 ms | Cache thrash |
| Not enough Workers (Go) | CPU 1.98 of 2; slots 3/4; about 304 queued | CPU-bound: add Workers |
| CPU constrained (Python) | CPU 1.02 of 1 (GIL); **Workflow Task** slots 10/10 with 10 s wait, while Activity slots sat at 6/20 | GIL-bound |
| Server-bound (Go, `make lab-slow-db`) | Postgres capped at 0.3 CPU, 16 pollers: polls 0.6–1.0 s; persistence p95 92–96 ms (healthy: 4 ms); completion RPC p95 2–4 s; throughput 135 → 18 Activities/s | Server-bound |

## Findings worth more than the presets

0. **Poll latency tells poller-bound from server-bound.** Both look the same
   from the Worker: tasks waiting, slots free, CPU idle. When it's poller-bound,
   the tasks are ready on the server, so each poll returns in **2–16 ms**. When
   it's server-bound, polls take **0.6–1.0 s** and the server's own persistence
   and completion-RPC latency rise 20× or more. The diagnosis checks the server
   *before* slots, because a slow server keeps slots occupied until its
   completion RPCs return.

   A server at 240% CPU is **not** evidence by itself. Under the heaviest mixed
   load here, with Temporal at 240% and Postgres at 135%, Workers waited 4 s with
   every slot free, which looked like the server. Raising pollers from 4 to 16
   cut schedule-to-start from **4.4 s to 84 ms**. It was poller-bound, the polls
   had been returning in 8–16 ms all along, and the server metrics were healthy.

1. **One poller handles more than you'd guess.** On a low-latency network,
   one Activity poller kept up with about 200 tasks/s before a queue formed.
   "Add pollers" is rarely the first fix. Poller limits show up when the
   network round trip to the server is long, which a laptop can't reproduce.
2. **A CPU-starved Worker looks poller-bound.** With CPU pinned, Activity slots
   were mostly *free*. The pollers were starved for CPU too, so tasks weren't
   being fetched. Read slot utilisation without CPU and you raise pollers, which
   fixes nothing. This is why the diagnosis checks CPU first.
3. **CPU-bound Activities starve Workflow Tasks.** On Go with one core, Workflow
   Task schedule-to-start hit 28 s while the Activity queue was short. On Python
   it's worse, and structural: Activity threads hold the GIL that the event loop
   running Workflow code also needs. Fix it with a `ProcessPoolExecutor` or a
   separate Worker process, not with more slots.
4. **The GIL changes what "CPU idle" means.** A Python Worker using 1.02 cores of
   a 2-core container is saturated. Measured against the container, it reads 51%
   and gets misdiagnosed. It did here, until the Python Worker started reporting
   the GIL's one core.
5. **Cache thrash costs little on short histories.** With a 0% hit ratio, replay
   p95 was 0.4 ms for 12-phase Workflows. The cost grows with history length,
   which is why thrash goes unnoticed until histories get long.
6. **The Go SDK's sticky cache holds one less than configured.** Internally it is
   `cache.New(cacheSize-1)`, so `SetStickyWorkflowCacheSize(2)` caches one
   Workflow, as the cache-size gauge showed.
7. **Don't trust one backlog number.** In steady runs, the server's
   `approximate_backlog_count` and the SDK-side estimate agreed (147 vs 162,
   57 vs 55). After a mass termination, though, the server gauge froze at 12
   for minutes while the SDK measured 39 s waits, a real queue of about 180.
   `DescribeTaskQueue` with `ReportStats` read 0 throughout on this 1.27
   server. The lab shows both numbers.

## Limits

- **One Worker per lane in the UI.** Scale with
  `docker compose --profile lab up -d --scale lab-worker-go=3`; the controller
  sums the Workers.
- **CPU ceilings:** the Go Worker container is capped at 4 CPUs and the Python
  one at 2, so the CPU presets can't starve the rest of the stack.
- **This is one laptop,** where network latency to the server is close to zero.
  Pollers and sync match behave differently across a real network.
- **Cleanup:** `make lab-down` terminates any lab Workflows still running.
  Otherwise they stay open and count against the stack's own stuck-Workflow
  monitoring.
