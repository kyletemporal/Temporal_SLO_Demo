# Java Worker tuning behind a feature flag (SDK 1.2x, manual pollers)

A Java Worker whose tuning comes from **one flag value**, and changes at
runtime without a redeploy. It runs as the `java` lane of the tuning lab, and
standalone against any Temporal cluster as the reference Worker for the k6 test
in [`../../k6/worker-tuning`](../../k6/worker-tuning).

Pinned to **Java SDK 1.28.4** on purpose. Every finding below was measured on
that version unless it says otherwise. Build any other version with
`--build-arg TEMPORAL_SDK_VERSION=…` (1.29.0 is built and tested too).

## What you copy into your own Worker

Three files in `src/main/java/io/temporal/tuning/`, with no lab dependency:

| File | What it does |
|---|---|
| `TuningFlag.java` | The flag: mode + values, strict JSON parsing, and validation. **The validation rules are the landmine list.** |
| `SwappableWorker.java` | Applies a flag by starting a new `WorkerFactory` before draining the old one. Rate-limits swaps. Exports the live flag as metrics. |
| `FileFlagSource.java` | Watches a JSON file (a mounted ConfigMap) by content hash. |
| `GenerationalGaugeReporter.java` | **Required with `SwappableWorker`.** Without it, slot gauges freeze across a swap (see landmine 5). |

Your flag service (LaunchDarkly, Unleash, a ConfigMap) only has to produce a
`TuningFlag` and call `swappableWorker.submit(flag)`. In the lab, the lab
controller plays that role.

## The flag

Annotated below. The parser is strict and **rejects comments**, so start from
[`tuning.example.json`](tuning.example.json).

```jsonc
{
  "mode": "MANUAL",                 // SDK_DEFAULTS | MANUAL | RESOURCE_BASED
  "workflowTaskPollers": 4,         // TOTAL, split sticky/normal on Java
  "activityTaskPollers": 4,
  "workflowTaskSlots": 100,         // RESOURCE_BASED: the tuner's maximum
  "activitySlots": 100,
  "stickyCacheSize": 600,
  "maxWorkflowThreads": 600,        // must be >= stickyCacheSize, see landmine 2
  "resource": { "targetCpu": 0.8, "targetMemory": 0.8 },   // RESOURCE_BASED only
  "label": "CHG-1234"               // free text; a label-only change does NOT swap
}
```

| Mode | What it sets | Use it for |
|---|---|---|
| `SDK_DEFAULTS` | Nothing. On 1.2x that's 5 + 5 pollers, 200 + 200 slots, cache 600, 600 threads | The control group: flip to it to prove the tuning is worth having |
| `MANUAL` | Fixed pollers and slots | **The only way to set pollers before 1.30.0**, which introduced `PollerBehaviorAutoscaling` |
| `RESOURCE_BASED` | `ResourceBasedTuner` sizes slots from CPU and heap. Pollers stay fixed | CPU- or memory-heavy Activities, with the caveats in landmine 4 |

Run it standalone:

```bash
docker run -e FLAG_SOURCE=file -v $PWD/flags:/config \
  -e TEMPORAL_ADDRESS=<ns>.<acct>.tmprl.cloud:7233 -e TEMPORAL_NAMESPACE=<ns>.<acct> \
  -e TEMPORAL_API_KEY=... -e TASK_QUEUE=tuning-test <image>
# edit flags/tuning.json -> the Worker swaps within ~2 s (+ TUNING_MIN_SWAP_INTERVAL_S)
```

Connection is `TEMPORAL_API_KEY` (TLS + API key), `TEMPORAL_TLS_CERT` +
`TEMPORAL_TLS_KEY` (mTLS), or neither (plaintext). Metrics are served on
`:9464/metrics`.

## Managing pollers by hand (no autoscaler)

On 1.2x you set pollers yourself, and nothing measures how many polls are
actually open (landmine 1). The approach that held up in the lab:

1. **Start small: 2–4 per type, and never more than slots.** A Java poller
   reserves a slot *before* it polls (`ActivityPollTask.poll`: `reserveSlot`,
   then `pollActivityTaskQueue`), so pollers beyond slots never poll.
2. **Raise pollers only on this evidence, all at once:** schedule-to-start is
   rising, slots are free, Worker CPU is not pinned, **and** the SDK's poll
   latency (`temporal_long_request_latency`, `PollActivityTaskQueue`) is
   **fast, 2–16 ms**. Fast polls with work waiting mean the server had the task
   ready and the Worker wasn't asking.
3. **Slow polls (0.6–1.0 s) with work waiting mean the server is slow, not
   the pollers.** More pollers make it worse. Check persistence latency.
4. **Check CPU before pollers.** A CPU-starved Worker starves its own pollers,
   so it looks poller-bound with free slots.
5. **Expect fewer than you'd guess.** On the lab's low-latency network, one
   Activity poller (Go lane) kept up with about 200 tasks/s. Pollers matter
   more as the round trip to the server grows, for example from on-prem to
   Cloud.
6. **Java WFT pollers are a total, not a per-queue count.** Java splits them
   between sticky and normal (`StickyQueueBalancer`). Go adds sticky pollers on
   top, so the same number on Go means more polls. 1 is raised to 2.

## Landmines, each measured

1. **No poller count on 1.2x.** `num_pollers` arrived in 1.30.0. `DescribeTaskQueue`
   counts poller *identities*, one per process: a Worker configured with 2
   Activity pollers read **`pollers: 1`**. Export the configured value yourself;
   `SwappableWorker` does it as `worker_tuning_setting{setting="activity_task_pollers"}`.

2. **The Workflow thread pool caps the sticky cache.** Each cached Workflow
   parks a thread. With cache 1000 and `maxWorkflowThreads` 20, the cache held
   **19** Workflows, and **58%** of Workflow Tasks missed and replayed, with
   slots free and CPU idle. Raising threads to 1000 gave 40 cached (every open
   Workflow) and a **100%** hit rate within about 6 s. The forced-eviction
   counter *understates* this: it read 0.4/s while misses ran at 10/s. Watch
   `temporal_sticky_cache_size` sitting at threads − 1 instead. Each thread is
   stack memory **outside the heap**.

3. **Micrometer clamps latency at 30 s.** The default timer histogram stops at
   30 s (verified: a 120 s sample lands in `+Inf`), so on a dashboard a
   2-minute backlog reads "30 s". `Main.java` raises the ceiling to 10 min with
   a `MeterFilter`; copy it.

4. **`ResourceBasedTuner` caps resources, not latency.** With a 60% CPU target
   it held system CPU at **0.586** by granting **1–2 of 50** Activity slots,
   while schedule-to-start grew **8.7 → 15.6 → 23.6 s**. Its CPU is
   `getSystemCpuLoad()`, the whole container or host, not this process. Its
   memory is heap / `-Xmx`, garbage included, blind to thread stacks. Its
   metrics have no `temporal_` prefix before 1.32 (`resource_slots_cpu_usage`).
   On **1.29.0–1.32.x** it can stop issuing slots permanently (sdk-java #2779,
   fixed in 1.33.0): the flag **refuses** RESOURCE_BASED there unless you set
   `acceptKnownTunerHangBug`.

5. **Slot gauges freeze across a swap.** With a tuner, 1.28.4 stops reporting
   `slots_available` for the ActivityWorker, and a plain
   `MicrometerClientStatsReporter` holds a gauge at its last value forever.
   Prometheus showed **10 free slots** while the tuner granted 1 and 300 tasks
   queued, so any "slots exhausted" alert stayed silent.
   `GenerationalGaugeReporter` removes gauges the new generation never reports.

6. **0 is not "none".** In `WorkerOptions`, 0 means "use the default" (5
   pollers, 200 slots). The flag rejects 0.

7. **A typo is not a no-op.** Lenient JSON leaves `"activityPollers": 8` at the
   default and reports success. The flag parses strictly, and **rejects** an
   unknown field.

8. **A rejected flag keeps the running config, except at boot.** At boot
   there is nothing to keep, so the process exits with code 2. The first
   version stayed up with **zero pollers** and looked healthy.

9. **API keys on 1.24–1.28.0 need `temporal-namespace` set by hand.** The SDK
   only sends it from 1.28.1. Without it Cloud answers `PERMISSION_DENIED:
   Request unauthorized` to a valid key. `Main.java` adds it on older versions.

10. **Micrometer must match the SDK.** 1.24–1.29 pull in `micrometer-core` 1.9.9.
    `micrometer-registry-prometheus` 1.13+ next to it fails at startup with
    `NoSuchMethodError`, and moved the registry class to another package. Pin
    the BOM (`pom.xml`).

11. **Three spellings of one task queue.** Java/Micrometer: `task_queue="lab-java"`.
    Go/tally: `task_queue="lab_go"`. Server: `taskqueue="lab_java"`. A dashboard
    variable joined across them matches nothing on one side. The Worker Tuning
    board has a separate server-side variable.

12. **`temporal_sticky_cache_total_forced_eviction` counts ordinary completions**
    (185 forced evictions for 185 completed Workflows). Don't alert on it.

13. **A swap costs one replay per open Workflow.** Measured at about 40 open,
    4/s: the hit ratio dipped to 75% for one 10 s window. The worst Workflow
    Task schedule-to-start across the swap was **101 ms**, and the 5 s
    sticky-timeout stall you might expect did not happen. Replay was cheap
    because histories were short, so measure it on yours. Swaps are
    rate-limited (`TUNING_MIN_SWAP_INTERVAL_S`, default 10) and coalesced, so a
    flapping flag can't thrash the cache. Size the container for both
    generations' threads during the drain.

14. **Kubernetes ConfigMaps:** a ConfigMap mounted with `subPath` **never
    updates** in a running pod, and kubelet propagation takes up to about a
    minute. `FileFlagSource` compares content hashes, because a ConfigMap
    update swaps a `..data` symlink that a file watcher doesn't see. *(These
    two are documented Kubernetes behaviour, not measured here.)*

## Tests

`mvn package` runs `TuningFlagTest`, one test per rule above that can be
checked offline, inside the Docker build. An image whose rules fail doesn't
build.
