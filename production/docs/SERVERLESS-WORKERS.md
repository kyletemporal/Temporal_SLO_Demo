# Serverless Workers — Temporal and AWS Lambda

**Short version: do not run a Temporal Worker inside Lambda. Run Lambda as the
Activity implementation, called from a long-lived Worker.**

That is not a style preference, it follows from what a Worker is. This document
explains why, gives the three patterns that do work, and — because it changes
your monitoring — covers what breaks in the SLO bundle when compute is
ephemeral.

---

## 1. Why a Worker is a bad fit for Lambda

A Temporal Worker is a **long-lived process that long-polls**. It opens a poll
against the Task Queue and holds it open — up to ~60 seconds — until a Task
arrives or the poll times out, then immediately re-polls. It is designed to be
running *before* work exists.

Lambda is the opposite shape: short-lived, invoked *by* an event, billed for
wall-clock duration, and capped at 15 minutes.

Put a Worker in Lambda and you get:

| | What happens |
|---|---|
| **Cost** | You pay for the long-poll wait. A Worker idling on an empty queue bills you for doing nothing — the exact workload serverless is supposed to eliminate. |
| **15-minute ceiling** | The Worker dies mid-flight. Any Activity still running is abandoned and must time out and retry. |
| **Sticky cache** | Gone on every invocation. Temporal caches Workflow state on the Worker that owns it; a fresh Worker must **replay the entire Workflow history** for each Workflow Task. On long histories this is expensive and it lands on your History service and datastore. |
| **Scaling is inverted** | Lambda scales in response to invocations. A Worker needs to already be polling for work to be delivered. Nothing invokes it when a Task arrives. |
| **Metrics** | Prometheus **cannot scrape a function that is not running.** Pull-based monitoring of Workers stops working entirely. |

The sticky-cache point is the one that bites hardest in production, and it gets
worse as your Workflows get longer — precisely as they mature.

---

## 2. The pattern that works: Lambda as the Activity

Keep a long-lived Worker fleet on ECS Fargate, EKS, or EC2. Let it own polling,
Workflow state, and the sticky cache. Let Lambda do the compute.

```
Workflow  ──schedules──▶  Activity  ──▶  Worker (Fargate, always polling)
                                              │
                                              └── invokes ──▶ Lambda
```

The Activity is a thin wrapper that invokes the function:

```go
func ProcessDocument(ctx context.Context, in Input) (Output, error) {
    // Heartbeat if the Lambda may run longer than a few seconds, so Temporal
    // can detect a stall rather than waiting out StartToCloseTimeout.
    activity.RecordHeartbeat(ctx, "invoking")

    out, err := lambdaClient.Invoke(ctx, &lambda.InvokeInput{
        FunctionName:   aws.String("process-document"),
        InvocationType: types.InvocationTypeRequestResponse,
        Payload:        payload,
    })
    if err != nil {
        return Output{}, err  // Temporal's retry policy takes it from here
    }
    if out.FunctionError != nil {
        // Distinguish "the function failed" from "the invoke failed" — only
        // one of them is worth retrying with the same input.
        return Output{}, temporal.NewNonRetryableApplicationError(
            string(out.Payload), "LambdaFunctionError", nil)
    }
    return decode(out.Payload)
}
```

What you keep: Temporal's retries, timeouts, heartbeats, and history. What you
gain: per-invocation billing and Lambda's scaling for the actual work. The
Worker fleet stays small because it is only orchestrating — it is not doing the
compute.

**Sizing:** a Worker fleet that only dispatches to Lambda is I/O-bound, not
CPU-bound. Raise `MaxConcurrentActivityExecutionSize` well above default before
adding Workers; a handful of Workers can hold thousands of in-flight
invocations. Watch task slots, not CPU.

---

## 3. When the Lambda runs longer than an Activity should block

For work that outlives a comfortable `StartToCloseTimeout`, do not hold the
Activity open. Use **async Activity completion**: the Activity fires the
invocation and returns "not done yet", and the Lambda completes the Activity
when it finishes.

```go
func StartLongJob(ctx context.Context, in Input) (string, error) {
    taskToken := activity.GetInfo(ctx).TaskToken

    _, err := lambdaClient.Invoke(ctx, &lambda.InvokeInput{
        FunctionName:   aws.String("long-job"),
        InvocationType: types.InvocationTypeEvent,   // async, returns immediately
        Payload:        withTaskToken(in, taskToken),
    })
    if err != nil {
        return "", err
    }
    // Tells Temporal: this Activity is in flight, do not mark it complete.
    return "", activity.ErrResultPending
}
```

The Lambda calls back when done:

```python
client.complete_activity_task(task_token=token, result=payload)   # or fail_activity_task
```

Set `ScheduleToCloseTimeout` to bound the whole thing, and have the Lambda
heartbeat for anything long-running so a silent death is detected rather than
waited out. **The task token must survive the trip** — pass it in the payload
and treat it as a credential, because anyone holding it can complete that
Activity.

This is also the right shape for Step Functions, Batch, or any other
fire-and-callback service.

---

## 4. The narrow case where a Lambda-hosted Worker is defensible

Low, bursty, latency-tolerant volume — a few Workflows an hour, where an
always-on Worker is genuinely wasteful.

Pattern: EventBridge invokes a Lambda on a schedule. The Lambda starts a
Worker, drains whatever is waiting, and exits before the ceiling.

Make it survivable:

- **Bound the run.** Exit at ~13 minutes, well inside the 15-minute limit.
- **Short poll timeouts** so the Worker is not still blocked on an empty poll
  when the clock runs out.
- **Low `MaxConcurrent*`** so you do not pick up more work than you can finish
  in the remaining budget.
- **Accept the latency.** Schedule-to-start is now bounded by your invocation
  cadence, not by Worker capacity. A 5-minute schedule means up to 5 minutes of
  schedule-to-start, permanently.
- **Expect ~100% sticky cache misses**, and keep Workflow histories short.

Be honest about what this is: you are emulating a long-lived process on
infrastructure designed not to have one, in exchange for not paying for idle. If
your volume ever becomes steady, move to pattern 2.

---

## 5. What ephemeral Workers do to this monitoring bundle

This is the part that gets missed, and it invalidates specific rules you have
just deployed.

### Delete `TemporalWorkerFleetAbsent`

Absence is the alert's whole premise, and with serverless Workers **absence is
the normal state**. The rule will page you constantly. Remove it for any
serverless Task Queue.

Replace it with backlog-based alerting, which is cluster-side and therefore
independent of whether any Worker exists right now:

```promql
# Work is queued and nobody is coming for it
sum by (namespace, taskqueue) (rate(no_poller_tasks[5m])) > 0
```

Plus a queue-age bound appropriate to your invocation cadence. The question
changes from *"are the Workers up?"* to *"is work being drained on schedule?"*,
which is the better question anyway.

### Re-scope or drop the `task_delivery` SLI

`task_delivery` measures Activity Tasks starting within 200ms. With scheduled
Workers, schedule-to-start is dominated by your invocation interval and cold
starts — 200ms is unreachable by construction and the SLO is permanently
breached for reasons that are not faults.

Either raise the boundary to match the cadence you actually promise (a 5-minute
schedule means an SLI boundary in minutes, not milliseconds), or drop the SLI
and express the promise as **backlog age** instead — which is what your customer
actually experiences.

Edit `S2S_LE` in the rule generator, or the `le=` selector in
`slo-rules.yml`, and pick a bucket boundary **that exists** in the histogram.

### Prometheus cannot scrape what is not running

Pull-based scraping of Lambda does not work. There is nothing listening between
invocations, and a Worker that lives 90 seconds will be missed by a 30-second
scrape more often than not.

Use a push path:

- **OTel exporter → OpenTelemetry Collector → Prometheus remote write.** The
  Temporal SDKs support an OTel metrics handler; the Collector gives you a
  stable scrape target that outlives the functions.
- Or **CloudWatch EMF** and read it back — workable, coarser, and it puts your
  Temporal metrics in a different system from your cluster metrics, which is a
  real cost during an incident.

Note this also changes which community dashboards apply: OTel-exported SDK
metrics use different metric names than Tally-exported ones (no `_seconds`
suffixes, different counter names). Pick the dashboard flavour that matches your
exporter or half its panels will be permanently dark.

### Cluster metrics carry more weight than ever

With ephemeral Workers, SDK metrics become a sampled, gappy signal. Everything
load-bearing should come from the cluster side — `no_poller_tasks`, sync match
rate, schedule-to-start, Workflow outcomes — because the Temporal Service is
always running and always reporting, whatever your compute is doing.

That is good practice with long-lived Workers. With serverless it is mandatory.

---

## 6. Choosing

| Your situation | Use |
|---|---|
| Steady volume, any latency requirement | **Pattern 2** — Fargate Workers, Lambda Activities |
| Work exceeding a comfortable Activity timeout | **Pattern 3** — async completion |
| A few Workflows an hour, minutes of latency acceptable | **Pattern 4** — scheduled Lambda Worker |
| Sub-second scheduling latency | Long-lived Workers. Serverless cannot deliver this. |

If you take one thing: **Lambda is excellent as the thing an Activity calls, and
poor as the thing that hosts a Worker.** Almost every "Temporal on Lambda"
problem dissolves once the polling loop lives somewhere with a heartbeat.
