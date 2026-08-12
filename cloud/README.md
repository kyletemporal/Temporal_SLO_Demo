# Temporal Cloud — SLOs, Error Budgets & Golden Signals

Monitoring for teams running on **Temporal Cloud**. Built on the Cloud
OpenMetrics endpoint and the [Temporal Cloud SLA](https://docs.temporal.io/cloud/sla).

```
prometheus/
  slo-rules.yml              5 SLIs, 28-day error budgets, burn-rate alerts (39 rules)
  alerts.yml                 Operational alerts incl. limits and throttling (11 rules)
  scrape-config.example.yml  OpenMetrics scrape config — four settings that matter
grafana/dashboards/
  temporal-cloud-golden-signals.json   RED + Saturation, SLOs on top
docs/
  SLA-AND-SLOS.md            What Temporal promises, and how to build on it
```

If you self-host instead, use [`../production/`](../production/) — the metric
names, and most of the PromQL, are different.

---

## Install

```bash
# 1. API key: Cloud UI -> Settings -> Service Accounts -> "Metrics Read-Only"
echo -n '<API_KEY>' > /etc/prometheus/temporal-cloud-api-key
chmod 600 /etc/prometheus/temporal-cloud-api-key

# 2. Scrape config — copy the temporal-cloud job into your prometheus.yml
cp prometheus/scrape-config.example.yml /etc/prometheus/

# 3. Rules
cp prometheus/slo-rules.yml prometheus/alerts.yml /etc/prometheus/
promtool check rules /etc/prometheus/*.yml

# 4. Dashboard — import via UI or provision from disk
```

Then replace the `REPLACE_ME` namespace filter and worker label in the scrape
config, and read §"Before you trust the numbers" below.

---

## Three things that make self-hosted PromQL wrong here

These are the reason this directory exists rather than pointing you at
`../production/`.

**1. Rates are pre-computed.** Every `_count` metric is a **gauge holding a
per-second rate**, already aggregated over a one-minute window. Wrapping it in
`rate()` is meaningless and returns small, plausible-looking numbers rather than
an error. Use `avg_over_time()` to widen a window — which is what every rule
here does.

**2. Percentiles are pre-calculated and cannot be re-aggregated.** There are no
histogram buckets, so `histogram_quantile()` does not apply. Temporal's docs are
explicit that aggregating `_p95` across dimensions produces a wrong number, and
a one-minute p95 cannot be widened into an accurate one-hour p95.

**3. There are no server internals.** You do not run frontend, history, matching
or a datastore, so there is no `persistence_latency`, no shard metrics, no
`service_name`. Saturation stops being CPU and datastore and becomes **limits**.

---

## What is measured

| SLI | Owner | Objective |
|---|---|---|
| `cloud_service_availability` | Temporal (mostly) | 99.9% |
| `cloud_start_workflow_latency` | Temporal + your call pattern | 99% |
| `workflow_completion` | **You** | 99% |
| `activity_completion` | **You** | 99% |
| `task_delivery` | **You** | 99.9% |

Split by owner on purpose. Only the first row is plausibly an SLA conversation;
the rest is your service running on someone else's infrastructure. Keeping them
separate is what lets you answer *"is it us or is it Temporal"* in seconds.

Expect **most of your budget to be spent by your own code.** Teams new to Cloud
often build only the first row and then have nothing useful to look at during an
incident.

---

## Before you trust the numbers

**Your availability SLI will not match Temporal's SLA calculation.** The SLA
excludes a specific list of error types — `NotFound`, `InvalidArgument`,
`PermissionDenied`, `Throttling`, `WorkflowExecutionAlreadyStarted` and others —
but `temporal_cloud_v1_service_error_count` is labelled by `operation` only.
**There is no error-type dimension**, so those exclusions cannot be applied.

Your number is a conservative superset: it counts errors the SLA forgives, so it
reads lower than Temporal's. That is fine for operating your service and wrong
for arguing a service credit. Temporal's calculation is authoritative.

**Do not promise more than you bought.** The contractual SLA is **99.9% on a
standard single-region Namespace** and **99.99% on a High Availability
Namespace**. If you offer your own customers an availability SLO, everything
Temporal owns sits underneath it. Promising 99.99% on a standard Namespace is
promising something you have not purchased — and composed availability is worse
than either component, so multiply rather than take the minimum.

The default objective is 99.9% to match the standard SLA. Raise
`cloud_service_availability` to 0.9999 only on an HA Namespace.

**Objectives are placeholders.** Run recording-only for two weeks, then set
numbers you can meet.

---

## Saturation means limits

The category with no self-hosted equivalent, and the one that surprises people
migrating. Hitting a limit looks like an outage to your users while sitting
comfortably inside Temporal's SLA — **`Throttling` is explicitly excluded from
it.**

Temporal exposes both sides of each ratio, so alert on the **ratio** and the
alert survives a limit increase:

| Usage | Limit |
|---|---|
| `temporal_cloud_v1_total_action_count` | `temporal_cloud_v1_action_limit` |
| `temporal_cloud_v1_operations_count` | `temporal_cloud_v1_operations_limit` |
| `temporal_cloud_v1_service_request_count` | `temporal_cloud_v1_service_request_limit` |
| `temporal_cloud_v1_service_pending_requests` | `temporal_cloud_v1_poller_limit` |

Also watch the throttle counters directly. A sub-minute burst can trigger
throttling while the averaged usage ratio still reads below the limit — if they
disagree, believe the throttle counter.

---

## Still scrape your own Workers

The Cloud endpoint stops at the Namespace boundary. It can tell you Tasks are
backing up; it cannot tell you your Worker pods are CPU-starved or that a slot
supplier is misconfigured.

The scrape config includes jobs for your SDK metrics and node-exporter. Without
them you are monitoring your provider, not your service — and "add Workers"
versus "raise `MaxConcurrent*`" stays a coin flip, exactly as it does
self-hosted.

---

## Version note

Targets the **v1** OpenMetrics endpoint (`metrics.temporal.io/v1/metrics`). The
older v0 PromQL endpoint uses `temporal_cloud_v0_*` names and lacks
`approximate_backlog_count`, `no_poller_tasks_count`, the activity metrics and
every limit gauge. Values differ between the two; where they differ
consistently, Temporal considers v1 more accurate. Mid-migration,
`metric_v1 or metric_v0` bridges a dashboard.

**Nothing here has been validated against a live Temporal Cloud account.** Rules
and dashboards pass `promtool` and every PromQL expression parses, and they are
built from the published metric reference — but the numbers themselves are
unverified. Treat the first week as a shakedown.
