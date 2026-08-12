# The Temporal Cloud SLA, and building your SLOs on top of it

Source of truth: <https://docs.temporal.io/cloud/sla>. Re-read it before any
commercial conversation; this is a working summary, not the contract.

---

## 1. What Temporal actually promises

| Namespace | Availability target | **Contractual SLA** |
|---|---|---|
| Standard, single region | 99.99% | **99.9%** against service errors |
| High Availability feature | 99.99% | **99.99%** against service errors |

Note the gap on the standard tier: the *target* is 99.99%, the *contract* is
99.9%. Design against the contract.

### How it is measured

All requests arriving in a Namespace are captured in **five-minute intervals**,
and the service-error rate is:

```
1 - (count of errors / count of requests)
```

Rates are **averaged per month and reset quarterly.**

Two things follow that people get wrong:

- **Averaging is per month.** A bad afternoon is diluted by the rest of the
  month. Your own burn-rate alerting will fire long before the SLA is
  threatened — which is the point. The SLA is a commercial floor, not an
  operational signal.
- **The window resets quarterly**, so a monthly figure does not roll forever.

### What counts as an error

Service errors — "such as the `UNAVAILABLE` gRPC status code".

### What does NOT count

These are explicitly excluded from the SLA calculation:

```
ClientVersionNotSupported   InvalidArgument        NamespaceAlreadyExists
NamespaceInvalidState       NamespaceNotActive     NamespaceNotFound
NotFound                    PermissionDenied       QueryFailed
RetryReplication            StickyWorkerUnavailable TaskAlreadyStarted
Throttling                  WorkflowExecutionAlreadyStarted
WorkflowNotReady
```

Read that list as a design document. It is Temporal drawing the line between
*"the platform failed"* and *"the caller asked for something impossible"* — the
same line the self-hosted bundle in this repo draws when it excludes
`Canceled`, `NotFound` and `InvalidArgument` from availability SLIs. Temporal
excludes `Throttling` too, which is worth noting: **being rate-limited is not an
outage**, it is you exceeding a limit you agreed to.

---

## 2. The thing that will trip you up

**You cannot reproduce the SLA calculation from the metrics.**

`temporal_cloud_v1_service_error_count` is labelled by `operation` only. There
is **no error-type dimension**, so none of the exclusions above can be applied
to it.

Consequences, in order of how much they matter:

1. **Your availability number will read lower than Temporal's.** Yours counts
   `NotFound`, `InvalidArgument`, `PermissionDenied` and throttling; the SLA
   does not. The gap is entirely caller-caused errors.
2. **Do not use your number to argue a service credit.** Temporal's calculation
   is authoritative. Use the [status page](https://status.temporal.io) and
   support, and treat your dashboard as the thing that told you to go look.
3. **Your number is still the more useful one operationally.** A spike in
   `InvalidArgument` is not Temporal's problem, but it is somebody's, and it is
   usually a deploy you just shipped.

If you want to narrow the gap, `temporal_cloud_v1_resource_exhausted_error_count`
and `temporal_cloud_v1_service_request_throttled_count` are separately exposed,
so throttling can be reasoned about on its own. Verify against your own data
before subtracting anything — whether throttled requests are also counted in
`service_error_count` is not something to assume.

---

## 3. Do not promise more than you bought

This is the one that ends up in a contract by accident.

If you run a platform on Temporal Cloud and offer your own customers an
availability SLO, everything Temporal owns sits underneath yours. On a
**standard Namespace you have a 99.9% contractual floor**, so:

- Promising your customers 99.99% means promising something you have not
  purchased. One Temporal incident inside its own SLA can blow your commitment
  and you have no recourse.
- Composed availability is *worse* than either component. Your service is
  Temporal **and** your Workers **and** your datastore **and** your network.
  Multiply, do not take the minimum.
- If you need 99.99%, buy the **High Availability** Namespace feature. That is
  the mechanism, and it moves the contractual floor to 99.99%.

A defensible position on a standard Namespace is to promise **99.9% or less** on
anything Temporal-dependent, and to hold the tighter numbers for the parts you
control end to end.

---

## 4. Splitting the error budget

The most useful thing you can do on Cloud is separate *whose fault it was*,
because unlike self-hosted you cannot fix the platform.

| SLI | Owner | What you do when it burns |
|---|---|---|
| `cloud_service_availability` | **Temporal** (mostly) | Check status.temporal.io, open a support ticket, consider an SLA claim |
| `cloud_start_workflow_latency` | Temporal + your call pattern | Check throttling and limits before blaming latency |
| `workflow_completion` | **You** | Your Workflow code, retry policies, dependencies |
| `activity_completion` | **You** | Your Activity code and its dependencies |
| `task_delivery` | **You** | Your Worker fleet — Cloud cannot poll a queue for you |

Only the first row is plausibly an SLA conversation. The rest is your own
service, running on someone else's infrastructure. Keeping them as separate
SLIs is what lets you answer "is it us or is it Temporal" in seconds rather
than during a bridge call.

Worth stating plainly: **most of your error budget will be spent by your own
code**, not by Temporal. Teams new to Cloud tend to build only the first row and
then have nothing to look at during an incident.

---

## 5. Saturation is a first-class concern on Cloud

Self-hosted, saturation means CPU and datastore. On Cloud it means **limits**,
and hitting one looks like an outage to your users while being entirely within
Temporal's SLA — `Throttling` is on the exclusion list.

Temporal exposes both sides of each ratio, which makes these directly
alertable:

| Usage | Limit |
|---|---|
| `temporal_cloud_v1_total_action_count` | `temporal_cloud_v1_action_limit` |
| `temporal_cloud_v1_operations_count` | `temporal_cloud_v1_operations_limit` |
| `temporal_cloud_v1_service_request_count` | `temporal_cloud_v1_service_request_limit` |
| `temporal_cloud_v1_service_pending_requests` | `temporal_cloud_v1_poller_limit` |

Alert on the **ratio**, not on absolute numbers, so the alert survives a limit
increase. And watch the throttle counters directly —
`temporal_cloud_v1_total_action_throttled_count`,
`_operations_throttled_count`, `_service_request_throttled_count` — because
those are the moment your users started feeling it.

One trap from Temporal's own docs: metrics are **per-second rates averaged over
a minute**, so a short burst can trigger throttling while the count metric still
reads below the limit. If throttle counters are non-zero but usage looks fine,
believe the throttle counter.

---

## 6. Percentiles need a traffic floor

Latency metrics are pre-calculated per 1-minute window. On a low-volume
Namespace that sample is tiny, so p50, p95 and p99 all converge on the slowest
single request. Temporal's guidance is that tail percentiles need roughly **20+
samples per window** to mean anything.

The `cloud_start_workflow_latency` SLI in this bundle is therefore gated on
`temporal_cloud_v1_service_request_count > 0.33` (~20 requests/minute). Below
that it records nothing rather than recording noise.

Widening your evaluation window does **not** fix this. A 1-minute p95 cannot be
re-aggregated into an accurate 1-hour p95 — the percentile is already computed
and the underlying distribution is gone.

For the same reason, never `sum()` or `avg()` a `_p95` across Namespaces,
operations or regions. Temporal's docs say this explicitly, and the resulting
number is not a percentile of anything.

---

## 7. v0 and v1

Two endpoints exist during the transition:

- **v0** — the older PromQL query endpoint, `temporal_cloud_v0_*`
- **v1** — the OpenMetrics endpoint, `temporal_cloud_v1_*`, richer labels
  (`region`, `temporal_task_queue`, `temporal_workflow_type`, `task_type`) and
  metrics that do not exist in v0 at all: `approximate_backlog_count`,
  `no_poller_tasks_count`, the activity metrics, and every limit gauge.

This bundle targets **v1**. Values will not match exactly between the two — the
aggregation and windowing differ, and where they differ consistently Temporal
considers v1 more accurate. If you are mid-migration, `metric_v1 or metric_v0`
bridges a dashboard across the gap.
