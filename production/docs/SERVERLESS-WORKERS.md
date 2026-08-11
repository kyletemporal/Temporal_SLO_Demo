# Serverless Workers — AWS Lambda and GCP Cloud Run

Temporal has **first-class support for running Workers on serverless compute.**
You do not build this yourself, and you should not use the old workarounds
(a Worker wrapped in a scheduled Lambda, or Lambda invoked from an Activity on
a long-lived fleet) unless you have a specific reason.

| Provider | Status |
|---|---|
| AWS Lambda | **Public Preview** |
| GCP Cloud Run | Pre-release — APIs may change; request access via support |

Primary sources, and the place to check before trusting anything here:

- <https://docs.temporal.io/serverless-workers>
- <https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers>
- <https://docs.temporal.io/production-deployment/worker-deployments/serverless-workers/self-hosted-setup>

This document covers what the feature is, what self-hosting it requires, and —
the part the official docs do not cover — **what it does to the SLOs and alerts
in this bundle.**

---

## 1. How it works

The thing that makes this work is that **Temporal starts the Worker**, rather
than a Worker process starting and polling forever.

A new server component, the **Worker Controller Instance (WCI)**, does the
starting. It is a system Workflow — one per Worker Deployment Version that has a
compute provider configured, running in your own Namespace.

```
1. A Task is submitted (StartWorkflow, ScheduleActivity)
2. Matching tries to hand it straight to a free Worker (sync match)
3. Sync match succeeds -> done
4. Sync match FAILS    -> Matching signals the WCI
5. WCI invokes the compute provider -> a Worker starts
6. Worker connects, polls, processes Tasks, shuts down gracefully
```

**Sync match failure is the primary control signal**, with Task Queue backlog
used for sizing. That is worth internalising: the metric this bundle already
alerts on as a symptom of an undersized fleet is the same signal Temporal uses
to *scale* a serverless fleet.

The two providers differ in execution model:

- **AWS Lambda** — the WCI calls `InvokeFunction` per unit of work. Each
  invocation starts a Worker, processes multiple Tasks, and shuts down before
  the invocation deadline. Each invocation is independent; **there is no shared
  state across invocations.** Scale-in is automatic — invocations simply end.
  Cold start is sub-second to low single-digit seconds, so reactive-only scaling
  does not build meaningful backlog.
- **GCP Cloud Run** — the WCI resizes a Worker Pool of long-lived instances that
  poll continuously, scaling to zero when idle. It targets ~80% utilisation so
  there is headroom to absorb arrivals, and is deliberately more conservative
  scaling in than out.

You register Workflows and Activities exactly as you would on a normal Worker.

---

## 2. Writing one

Official SDK packages, all following the same shape — a deployment version plus
a configure callback, returning a handler:

| SDK | Package | Entry point |
|---|---|---|
| Go | `go.temporal.io/sdk/contrib/aws/lambdaworker` | `RunWorker` |
| Python | `temporalio.contrib.aws.lambda_worker` | `run_worker` |
| TypeScript | `@temporalio/lambda-worker` | `runWorker` |
| Java | `io.temporal.aws.lambda.LambdaWorker` | `LambdaWorker.run` |
| .NET | `Temporalio.Extensions.Aws.Lambda` | `TemporalLambdaWorker.CreateHandler` |

```go
func main() {
    lambdaworker.RunWorker(worker.WorkerDeploymentVersion{
        DeploymentName: "my-app",
        BuildID:        "build-1",
    }, func(opts *lambdaworker.Options) error {
        opts.TaskQueue = "orders"
        opts.RegisterWorkflowWithOptions(OrderWorkflow, workflow.RegisterOptions{
            VersioningBehavior: workflow.VersioningBehaviorPinned,
        })
        opts.RegisterActivity(ChargePayment)
        return nil
    })
}
```

**Worker Versioning is required, not optional.** Every Workflow must declare a
versioning behaviour — `Pinned` or `AutoUpgrade` — either per-Workflow at
registration or as a worker-level default. If your Workflows are not already
versioned, that is the first piece of work, not an afterthought.

Two configuration points that bite:

- **Lambda function timeout.** Set it to at least your longest Activity
  `StartToClose` plus the worker stop timeout (7s default). The Go package docs
  recommend **a minimum of one minute**. Too short and the Worker is killed
  mid-Task.
- **Activity duration ceiling** on Lambda is 15 minutes minus the shutdown
  buffer. Work that legitimately runs longer needs async Activity completion
  (§5) or a different compute target.

TypeScript users: pre-bundle Workflow code with `workflowBundle` rather than
`workflowsPath`, so webpack bundling does not run on every cold start.

---

## 3. Self-hosting it — you need 1.31.0

**This is the gating fact for this repo.** Serverless Workers require
**Temporal Service v1.31.0 or later**. The demo in this repository pins
**1.26.2**, so it cannot run them without a server upgrade.

The WCI is **disabled by default** and enabled through dynamic config:

```yaml
workercontroller.enabled: true
workercontroller.compute_providers.enabled: [aws-lambda]
workercontroller.scaling_algorithms.enabled: [no-sync]
```

These can be scoped per-namespace with a `constraints` section.

Then the plumbing, all of which is genuinely easy to get wrong:

- **Network.** The Temporal frontend must be reachable *from the Lambda
  execution environment*. On a private cluster that means VPC access, peering,
  or equivalent — this is the most common reason a correctly configured Worker
  never connects.
- **IAM.** The Temporal server needs `sts:AssumeRole`. The role it assumes in
  your account needs `lambda:GetFunction` and `lambda:InvokeFunction` on the
  Worker functions, with a trust policy allowing the server's identity.
- **Credentials.** On AWS, attached instance/task/pod roles work automatically.
  Off AWS, use IAM Roles Anywhere or static credentials.

Follow the self-hosted setup guide rather than this summary; it is the part most
likely to have changed since writing.

---

## 4. The pattern Temporal recommends: spillover

Serverless Workers **can share a Task Queue with long-lived Workers**, acting as
spillover capacity for the fleet you already run.

This is the recommendation to lead with for most existing deployments. You keep
a baseline of long-lived Workers sized for steady traffic — with warm sticky
caches and predictable latency — and let serverless absorb the bursts you would
otherwise have to provision for permanently. It is also the lowest-risk way to
adopt a Public Preview feature: if serverless invocation stops working, the
long-lived fleet is still draining the queue.

Also recommended, to stop one bad Activity taking out unrelated work: **split
Workflow and Activity Workers into separate deployments, and set Activity slots
to 1 per Worker.**

---

## 5. When Lambda is still the wrong host

Serverless Workers do not make every workload suitable:

- **Work exceeding ~15 minutes.** Use async Activity completion: the Activity
  starts the job and returns "pending", and whatever runs the job completes the
  Activity by task token later. Bound the whole thing with
  `ScheduleToCloseTimeout` and heartbeat so a silent death is detected rather
  than waited out. Treat the task token as a credential.
- **Sub-second, consistently low scheduling latency.** Cold start is fast but
  not free, and the first Task after idle pays it.
- **Very long Workflow histories with heavy replay.** On Lambda each invocation
  is independent with no shared state, so sticky-cache benefits do not carry
  across invocations and replay cost lands on your History service and
  datastore. Cloud Run's long-lived pool does not have this problem.

---

## 6. What this does to the SLOs and alerts in this bundle

Not covered by the official docs, and the reason this file lives here. Ephemeral
Workers invalidate specific rules you may have just deployed.

### Delete or rewrite `TemporalWorkerFleetAbsent`

Absence is the alert's entire premise, and with serverless Workers **absence is
the normal steady state** — scale-to-zero is the feature. The rule will page you
constantly. Remove it for any serverless Task Queue.

Replace it with backlog-based alerting, which is cluster-side and therefore
independent of whether any Worker exists right now:

```promql
sum by (namespace, taskqueue) (rate(no_poller_tasks[5m])) > 0
```

The question changes from *"are the Workers up?"* to *"is work being drained?"*,
which is the better question anyway. Note the subtlety: sync match failure is
now both your alert signal **and** Temporal's scale-up trigger, so a brief spike
is the system working. Alert on sustained backlog, not on the first failure.

### Re-scope `task_delivery`

`task_delivery` measures Activity Tasks starting within 200ms. With scale-from-
zero, the first Task after an idle period pays cold start, so a 200ms objective
is unreachable by construction and the SLO is breached for reasons that are not
faults.

Either raise the boundary to something you would actually promise, or express
the SLI as **backlog age** — what the customer experiences — instead of
schedule-to-start. Edit `S2S_LE` in `tools/generate_production_rules.py` and
pick a bucket boundary that exists in the histogram.

### Prometheus cannot scrape what is not running

Pull-based scraping does not work against Lambda: there is nothing listening
between invocations, and a Worker that lives 90 seconds will be missed by a
30-second scrape more often than not.

Observability in the Lambda worker packages is **opt-in**, and the Go package
ships ready-made OTel configuration for AWS Distro for OpenTelemetry under
`contrib/aws/lambdaworker/otel/`. Use it: export via OTel to a Collector, and
remote-write to Prometheus. The Collector is the stable scrape target that
outlives the functions.

One knock-on: OTel-exported SDK metrics use **different metric names** than
Tally-exported ones (no `_seconds` suffixes, different counter names). Any
dashboard or rule in this bundle that references `temporal_*_seconds` needs the
OTel spelling, and the community SDK dashboards come in both flavours — pick the
one matching your exporter or half the panels stay dark.

### Cluster metrics carry more weight than ever

With ephemeral Workers, SDK metrics are a sampled, gappy signal. Everything
load-bearing should come from the cluster side — `no_poller_tasks`, sync match
rate, schedule-to-start, Workflow outcomes — because the Temporal Service is
always running and always reporting, whatever your compute is doing.

Good practice with long-lived Workers. Mandatory with serverless.

### Watch the WCI itself

It is a Workflow in your Namespace, so it is inspectable:

```bash
temporal workflow list --namespace <NS> \
  --query 'TemporalNamespaceDivision = "TemporalWorkerControllerInstance"'

temporal workflow show --namespace <NS> \
  --workflow-id 'temporal-sys-worker-controller-instance:<DEPLOYMENT>:<BUILD_ID>'
```

If Tasks are queuing and no Lambda invocations appear in CloudWatch, the WCI is
where the failure is. Its history shows recent Activity results.

---

## 7. Two operational caveats worth planning around

**Namespace failover does not move your compute.** On failover, the WCI keeps
invoking Workers in the original region unless you manually repoint the compute
provider. If you rely on multi-region failover for availability, this is a
manual step in your runbook — not automatic.

**Public Preview means APIs may change.** For AWS Lambda that is an accepted
risk for many teams; Cloud Run is Pre-release and explicitly may change in
backwards-incompatible ways. The spillover pattern in §4 is the hedge: keep a
long-lived fleet that can carry the queue on its own.
