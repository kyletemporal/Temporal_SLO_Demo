# Temporal Self-Hosted — Observability Guide & Dashboard Template

**Version:** 1.0
**Audience:** Platform / SRE teams operating a self-hosted Temporal Service
**Deliverables in this package:**

| File | Purpose |
|---|---|
| `docs/OBSERVABILITY-GUIDE.md` | This document — what to watch, why, and how to act on it |
| `grafana/dashboards/custom/temporal-self-hosted-overview.json` | Grafana dashboard (13 panels, 4 sections), auto-provisioned by the demo stack |
| `prometheus/prometheus.yml` | Scrape configuration for cluster + SDK metrics |
| `prometheus/alerts.yml` | Prometheus alerting rules matching the dashboard panels |
| `SETUP.md` | Step-by-step setup and troubleshooting for the runnable demo stack |

---

## 1. Scope

This is a deliberately minimal starting point, not a complete observability platform. It covers the signals that answer the four questions that come up in almost every Temporal incident:

1. **Is the Temporal Service healthy?** (request rate, error rate, latency)
2. **Is work reaching Workers?** (matching / task queue health)
3. **Can Workers keep up?** (schedule-to-start latency, slot saturation)
4. **Is the datastore keeping up?** (persistence latency and errors)

Everything else — per-Workflow-type breakdowns, cost attribution, sticky cache tuning, replication — is intentionally left out of v1. Add it when you have a question the base dashboard cannot answer. A dashboard nobody reads is worse than no dashboard, and every panel you add makes the ones that matter harder to find.

---

## 2. Metric sources: what changes vs. Temporal Cloud

This is the single most common source of confusion when moving between Cloud and self-hosted guidance. There are **three distinct metric families**, and queries are not portable between them.

| Source | Prefix / example | Emitted by | `rate()` valid? | Available self-hosted? |
|---|---|---|---|---|
| **Cluster metrics** | `service_requests`, `persistence_latency` | The Temporal Service itself (frontend, history, matching, worker roles) | **Yes** | **Yes — this is your primary source** |
| **SDK metrics** | `temporal_workflow_task_schedule_to_start_latency` | Your Worker and Client processes | **Yes** | **Yes — but only if you enable the exporter in application code** |
| **Cloud metrics** | `temporal_cloud_v1_*` | Temporal Cloud's OpenMetrics endpoint | **No** — already pre-computed rates | **No — does not exist self-hosted** |

**Practical implication:** any Temporal Cloud dashboard or query you have seen referencing `temporal_cloud_v1_*` will return nothing on a self-hosted cluster. The equivalent signals exist, but under different names, and they require `rate()` where the Cloud versions do not. The mappings used in this dashboard:

| Signal | Temporal Cloud | Self-hosted equivalent |
|---|---|---|
| Service request volume | `temporal_cloud_v1_service_request_count` | `rate(service_requests{service_name="frontend"}[…])` |
| Service errors | `temporal_cloud_v1_service_error_count` | `rate(service_error_with_type{service_name="frontend"}[…])` |
| Service latency | `temporal_cloud_v1_service_latency_p99` | `histogram_quantile(0.99, rate(service_latency_bucket[…]))` |
| Sync match rate | `poll_success_sync_count / poll_success_count` | `rate(poll_success_sync[…]) / rate(poll_success[…])` |
| Poll success rate | `poll_success_count / (poll_success_count + poll_timeout_count)` | `rate(poll_success[…]) / (rate(poll_success[…]) + rate(poll_timeouts[…]))` |
| Workflow failures | `temporal_cloud_v1_workflow_failed_count` | `rate(workflow_failed[…])` |
| Backlog depth | `temporal_cloud_v1_approximate_backlog_count` | **No direct equivalent in older versions.** Use `no_poller_tasks` + `asyncmatch_latency` + SDK schedule-to-start latency. Newer server versions expose an approximate backlog gauge — verify against your build. |
| Persistence health | *(not exposed to Cloud customers)* | `persistence_latency_bucket`, `persistence_error_with_type` — **self-hosted only, and one of the most valuable signals you have** |

> **Self-hosted advantage worth using.** Persistence metrics are not visible to Cloud customers because Temporal operates that layer for them. You own the datastore, so you get direct visibility into it — and on self-hosted deployments the datastore is the bottleneck far more often than Temporal itself. The Persistence panels are not optional extras; treat them as first-class.

### 2.1 SDK histogram units — read this before setting thresholds

| SDK | Histogram unit | Prometheus metric suffix | 200ms threshold is written as |
|---|---|---|---|
| Go, Java | **seconds** | `…_latency_seconds_bucket` | `0.2` |
| TypeScript, Python, .NET | **milliseconds** | `…_latency_bucket` | `200` |

**The shipped dashboard and alert rules assume Go/Java (seconds).** If your Workers are on TypeScript, Python, or .NET, you must edit panels 8 and 9 and the two schedule-to-start alert rules — drop `_seconds` from the metric name, change the panel unit from `seconds` to `milliseconds`, and multiply the thresholds by 1000. Getting this wrong produces thresholds that are off by a factor of 1000 and alerts that either never fire or never stop.

---

## 3. Collection setup

### 3.1 Temporal Service metrics

Expose the Prometheus listener in your Temporal Server configuration under `global.metrics.prometheus.listenAddress`. Every service role (frontend, history, matching, worker) emits its own metrics and must be scraped independently — scraping only the frontend hides the persistence and matching signals that drive most incidents.

Dev server, for local validation:

```bash
temporal server start-dev --metrics-port 8000
```

If you deployed via the Temporal Helm chart, use the ServiceMonitor / PodMonitor resources it ships rather than the static scrape config — they handle role discovery for you.

### 3.2 SDK metrics

**These do not exist until you turn them on in application code.** No amount of Prometheus configuration will produce them. Each SDK has its own mechanism; all of them amount to configuring a Prometheus exporter on the Client/Runtime options and exposing a port (8077 by convention).

Reference samples:

- Go — `github.com/temporalio/samples-go/tree/main/metrics`
- Java — `github.com/temporalio/samples-java` → `core/.../samples/metrics`
- Python — `github.com/temporalio/samples-python/tree/main/prometheus`
- TypeScript — `github.com/temporalio/samples-typescript/tree/main/interceptors-opentelemetry`
- .NET — `github.com/temporalio/samples-dotnet/tree/main/src/OpenTelemetry`

Without this step, the **Worker Fleet Health** section of the dashboard will be empty. That section contains the schedule-to-start latency signals, which are the most direct measure of whether your application is actually keeping up — so this is not a step to defer.

### 3.3 Health checks

The Frontend Service supports TCP or gRPC health checks on **port 7233** (not 8080, and not the metrics port). Wire this into your load balancer and orchestrator liveness probes.

### 3.4 Scrape interval

15s is a reasonable default and is what `prometheus.yml` ships with. Going below 10s multiplies cardinality cost for little diagnostic benefit at these thresholds.

---

## 4. Dashboard panel reference

Import `temporal-self-hosted-overview.json` via **Dashboards → New → Import**, then select your Prometheus data source when prompted.

Template variables: `$namespace` (multi-select) and `$task_queue` (multi-select, SDK metrics only).

### Section 1 — Service Health (Temporal Cluster)

| Panel | Question it answers | Target | If it moves |
|---|---|---|---|
| **Frontend Availability** | Is the Temporal API serving requests successfully? | > 99.9% | Drop to the Errors by Type panel to classify |
| **Frontend Request Rate by Operation** | What is the shape of the load? | *(baseline)* | Context panel — correlate load changes against latency and error movement |
| **Frontend Errors by Type** | What kind of failures? | ~0 for `Internal` / `Unavailable` | `ResourceExhausted` → throttling or overload. `NotFound` / `AlreadyExists` are often normal application behaviour — baseline before alerting |
| **Frontend P95 Latency by Operation** | Is the API slow? | Workload-dependent; baseline it | Check Persistence latency **first** — the datastore is upstream of nearly all cluster latency |

> Requires Temporal Server **v1.17.0+** for `service_error_with_type`. On older versions substitute `service_errors`, which lacks the `error_type` breakdown.

### Section 2 — Task Queue Matching Health

| Panel | Question it answers | Target | If it moves |
|---|---|---|---|
| **Sync Match Rate** | Are Tasks handed straight to a waiting Worker, or persisted first? | > 99%, alert < 95% | Low value = no Worker was ready. Combined with high schedule-to-start latency, the fleet is undersized or under-resourced |
| **Poll Success Rate** | Are polls returning work, or timing out empty? | > 95%, alert < 90% | Low value **with** low schedule-to-start latency and idle Worker hosts = too many pollers, not too few. Scale *down* |
| **Tasks With No Poller** | Is work being queued where nobody is listening? | **Exactly zero** | Almost always a Task Queue name mismatch between starter and Worker, or a Worker deployment that is fully down |

> **"Tasks With No Poller" is the highest signal-to-noise panel on this dashboard.** It has essentially no false-positive mode. A sustained non-zero value means work is accumulating and will not progress on its own.

### Section 3 — Worker Fleet Health (SDK metrics)

| Panel | Question it answers | Target | If it moves |
|---|---|---|---|
| **Workflow Task Schedule-to-Start P99** | How long do Workflow Tasks wait for a Worker? | Near zero. Plot 100ms, alert 200ms | The primary backlog signal. Read together with Sync Match Rate to decide between "add Workers" and "fix Workers" |
| **Activity Schedule-to-Start P99** | Same, for Activity Tasks | Near zero. Plot 100ms, alert 200ms | **Verify `ScheduleToStartTimeout` is not set in Activity Options** — it truncates this metric and will mislead you |
| **Worker Task Slots Available (min)** | Are Workers saturated? | > 0 at all times | Zero slots **with** high host CPU = genuine capacity shortage, add Workers. Zero slots **with** idle host CPU = concurrency limits set too low, raise `maxConcurrentWorkflowTaskExecutionSize` / `maxConcurrentActivityExecutionSize` |

### Section 4 — Persistence & Workflow Outcomes

| Panel | Question it answers | Target | If it moves |
|---|---|---|---|
| **Persistence P95 Latency by Operation** | Is the datastore keeping up? | Workload-dependent; baseline it | The most common root cause of cluster-wide latency on self-hosted. Investigate before touching Temporal configuration |
| **Persistence Errors by Type** | Can Temporal reach its datastore? | Zero | Connection pool exhaustion, storage saturation, network path, or credential expiry |
| **Workflow Outcomes** | Are applications succeeding? | Failures near zero | Temporal's guidance is that Workflows should be designed to always succeed — a persistent failure line is a signal about application error handling, not cluster health |

---

## 5. Alerting

`prometheus/alerts.yml` ships eight rules mapped 1:1 to the panels above.

**Do not promote any of these to a paging severity until you have baselined them against two weeks of your own traffic.** Shipped thresholds are conservative on purpose. The failure mode of a v1 alerting config is not missing an incident — it is firing often enough that the team learns to ignore it, at which point the alerts are worse than nothing.

Suggested rollout:

1. **Week 1–2** — deploy dashboard only. Watch. Record actual baselines for latency, error rate, and request volume.
2. **Week 3** — enable alerts at `severity: warning`, routed to a channel, not a pager.
3. **Week 4+** — promote to paging only the rules that fired exclusively on real problems. Typically that first set is: `TemporalTasksWithNoPoller`, `TemporalPersistenceErrors`, and one schedule-to-start rule.

---

## 6. Triage runbook

Start from the symptom, not from the dashboard.

### "Workflows are slow to start / work is backing up"

1. **Schedule-to-Start P99** (Section 3) — is it elevated? If no, the delay is inside Activity execution or a downstream dependency, not in Temporal. Stop here.
2. **Sync Match Rate** (Section 2) — this is the fork:
   - **High sync match + high schedule-to-start** → not enough Worker capacity. Check Worker host CPU/memory, then add Workers or raise concurrent pollers.
   - **Low sync match + high schedule-to-start** → Tasks are queuing before delivery. Check **Task Slots Available**: zero slots with idle CPU means concurrency limits are too low; zero slots with busy CPU means genuine capacity shortage.
3. **Tasks With No Poller** — non-zero means it is a routing problem, not a capacity problem. Verify Task Queue names match between the Workflow starter and the Worker registration. No amount of scaling fixes this.

### "Everything is slow"

1. **Persistence P95 Latency** (Section 4) first, every time. On self-hosted deployments the datastore is the bottleneck more often than Temporal is.
2. **Persistence Errors by Type** — non-zero indicates connectivity or saturation, not slowness.
3. Only after both are clean, look at **Frontend P95 Latency** for a Temporal-side cause.

### "Poll success rate is low but nothing else looks wrong"

Check whether **Schedule-to-Start latency is also low** and **Worker host utilization is low**. All three together mean you have too many pollers competing for too little work. Reduce Worker count or concurrent pollers per Worker. This costs real money to leave in place and produces no user-visible symptom, so it is easy to miss.

### General principle

Check whether existing Workers have free slots **before** adding Workers. Slot exhaustion with idle host CPU is a configuration problem, and adding hosts makes it more expensive without making it faster.

---

## 7. Known gaps in v1

Documented explicitly so nobody assumes coverage that is not there:

| Gap | Why it is deferred | Add when |
|---|---|---|
| Backlog depth gauge | No stable equivalent across all self-hosted versions; approximate backlog metrics vary by server build | You confirm the metric exists on your server version — check `/metrics` on the matching service |
| Sticky cache size / evictions | Useful for Worker tuning, noisy without a tuning exercise underway | You start a Worker performance tuning engagement |
| Per-Workflow-type breakdowns | High cardinality, and unhelpful until you know which Workflow to look at | You have a specific Workflow under investigation |
| History / matching service internals (`task_latency_*`, `task_attempt`) | Deep-dive metrics, not daily-driver signals | You are debugging history service throughput specifically |
| Replication lag | Multi-cluster deployments only | You run multi-cluster replication |
| Host-level resource metrics (CPU, memory, disk) | Different collector (node_exporter / cAdvisor), outside Temporal's metrics | Immediately — the runbook above repeatedly requires "check Worker host CPU", so this dashboard is materially less useful without it alongside |

> The last row is the one that matters most. Several triage branches in Section 6 depend on knowing Worker host utilization. If you do not already have node-level metrics in Grafana, get them in place alongside this dashboard.

---

## 8. Verification checklist

Setup is not complete when the config looks right — it is complete when data is queryable in Grafana. Work through this in order.

**Generate traffic before you start.** Many Temporal metrics are counters, and a counter does not exist until the event it counts has happened. Verifying an idle stack reports healthy metrics as missing and sends you debugging a problem you do not have. On the demo stack, `make smoke` is enough to seed them; against a real cluster, use real traffic.

- [ ] `http://<prometheus>:9090/targets` shows **all** Temporal service roles UP (not just frontend)
- [ ] `http://<prometheus>:9090/targets` shows Worker SDK targets UP
- [ ] Query `service_requests` in Prometheus — returns series with recent timestamps
- [ ] Query `temporal_worker_task_slots_available` — returns series (confirms SDK exporter is live, not just the port)
- [ ] Query `poll_success_sync` — returns series. If empty, confirm traffic has actually flowed first; this is a counter and is legitimately absent on an idle cluster. If it is still empty under load, the metric name differs on your server version — check `/metrics` on the matching service and update the Sync Match Rate panel accordingly
- [ ] Dashboard imported, `$namespace` variable populates with your actual namespaces
- [ ] `$task_queue` variable populates with your actual Task Queue names
- [ ] Every panel that *should* have data under load does. Four are correctly empty on a healthy system and must not be "fixed": *Tasks With No Poller* (its title says expect zero), and the failed / timeout / cancel series of *Workflow Outcomes*. If those are populated, you have a real problem — not a provisioning one
- [ ] SDK unit check: confirmed Go/Java (seconds, as shipped) or edited panels 8/9 and the two latency alert rules for millisecond SDKs
- [ ] Alert rules loaded: `http://<prometheus>:9090/rules` shows the three rule groups
- [ ] Node-level metrics for Worker hosts are available in the same Grafana instance

An *unexpectedly* empty panel is a broken panel. Chase every one down before declaring this live — a dashboard with three dead panels teaches the team to distrust the other ten. The corollary matters just as much: know which panels are supposed to be empty, and say so on the panel itself. A panel that is blank by design and undocumented is indistinguishable from one that is blank by accident.

---

## 9. Reference

- Temporal self-hosted monitoring guide: `docs.temporal.io` → Production Deployment → Self-Hosted Guide → Monitoring
- Full cluster metric definitions: `github.com/temporalio/temporal` → `common/metrics/metric_defs.go`
- SDK metrics reference: `docs.temporal.io` → References → SDK metrics
- Community Grafana dashboards: `github.com/temporalio/dashboards`
- Datadog Temporal integration: `docs.datadoghq.com/integrations/temporal/`
