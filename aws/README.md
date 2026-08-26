# Temporal on AWS

Two pieces a platform team hits in week one: getting Workflow history out of
Temporal's retention window, and running Workers on EKS so they scale on the
right signal.

| | |
|---|---|
| [`terraform/export-sink`](terraform/export-sink) | S3 bucket, IAM role and the Temporal Cloud export sink |
| [`k8s/worker`](k8s/worker) | Worker Deployment, IRSA service account, PDB, and **two** autoscaling options — KEDA (`scaledobject.yaml`) or a plain HPA (`hpa.yaml`) |
| [`k8s/karpenter`](k8s/karpenter) | NodePool + EC2NodeClass provisioning the nodes underneath the fleet |

## Verification status

Terraform passes `init` and `validate` against real provider schemas (AWS 6.x,
temporalcloud 1.7). Kubernetes manifests parse cleanly and were checked for the
specific mistakes documented below. **Nothing has been applied**, and neither
piece can be meaningfully verified without an AWS account, a Temporal Cloud
account and a cluster. Each section below lists what you must verify yourself.

---

## S3 export sink

Temporal Cloud retention caps at **90 days**. Export writes closed Workflow
histories to your own bucket hourly — for compliance, audit, or replaying a
Workflow in a debugger two years later.

### Three things that will bite first

**The bucket must be in the same region as the Namespace.** Temporal states this
as a requirement. There is a `precondition` on the sink, because a mismatch
provisions cleanly and then silently never delivers.

**The IAM role must exist before the sink.** The Cloud UI offers a CloudFormation
flow that creates it, but Temporal's docs say plainly: *"Please pre-create the
role if setting up Export via terraform/tcld."* This module creates it and orders
it with `depends_on`.

**The trust principal is a required input, not a constant.** Temporal Cloud
writes using **multiple intermediary IAM roles chosen at random** — for security
isolation, load distribution and failover. That set is account- and
region-specific, and Temporal can rotate it.

Hardcoding a guess would produce a trust policy that works until it silently
stops. Get the real values from the CloudFormation template the Cloud UI
generates:

> Namespace → Export → Configure → Access method: **Manual** → Template URL

### Verify it yourself

Provisioning a sink is not evidence that anything is delivered.

```bash
terraform output -raw validate_command   # prints the command below, filled in
temporal cloud namespace export s3 validate \
  --namespace <ns.acct> --sink-name <name> \
  --role-arn <role-arn> --bucket-name <bucket> --region <region>
```

Then wait an hour, close a Workflow, and confirm objects actually appear in the
bucket. Export runs **hourly**, so an empty bucket ten minutes after setup means
nothing either way.

### Cost

Each exported Workflow accrues **one Action**, billed per namespace. It scales
with Workflow volume, not data volume — a high-throughput namespace of short
Workflows costs more to export than a low-throughput one of long Workflows.

---

## EKS Worker deployment

### Do not autoscale a Worker on CPU

This is the single most important thing in `k8s/worker`.

A Worker spends most of its life blocked in a **long poll**, which costs almost
no CPU. When the queue backs up and the fleet is falling behind, CPU stays flat
or falls — so a CPU-based HPA **scales down the fleet that is already behind.**
It is not a weak signal, it is an inverted one.

Scale on **schedule-to-start latency** instead: how long a task waits before a
Worker picks it up. It directly measures "is the fleet keeping up", and it
responds immediately to adding capacity. `scaledobject.yaml` uses KEDA, with task
backlog as a leading secondary signal.

### Other Worker-specific choices, and why

| Choice | Why |
|---|---|
| **No readiness probe** | A Worker serves no traffic — it polls outbound. Nothing routes to it, so readiness is the wrong question. |
| **Liveness does not test Temporal connectivity** | Restarting a Worker that cannot reach Temporal does not fix the network; it discards the sticky cache and forces every Workflow to replay when connectivity returns. |
| **No CPU limit** | CPU throttling raises Workflow Task latency with no obvious cause — it looks like slow Workflow code. Memory limit yes (OOM is real), CPU limit no. |
| **`terminationGracePeriodSeconds: 120`** | The default 30s kills long Activities mid-flight, turning every rolling deploy into a burst of Activity timeouts and retries. Set above your longest Activity `StartToCloseTimeout`. |
| **No `replicas` in the Deployment** | KEDA owns replica count. Setting both makes them fight and the fleet oscillate. |
| **PodDisruptionBudget** | Without one, a node drain can evict every Worker at once, leaving the queue unpolled — which looks exactly like an application outage. |
| **pprof not in the Service** | It exposes goroutine stacks and heap contents. Reach it with `kubectl port-forward`. |

### The trigger deliberately NOT used

Worker slot availability. `temporal_worker_task_slots_available` is emitted **by
the Workers**, so scaling on it is a feedback loop: add Workers, total slots
rise, the metric improves, and the autoscaler concludes it was right — whether or
not a single task moved faster. Scale on the **queue's** state, not the fleet's.

### Self-hosted vs Cloud

The backlog trigger uses `temporal_cloud_v1_approximate_backlog_count`, which
**does not exist on a self-hosted cluster**. There, the query returns nothing and
KEDA reports the trigger inactive — indistinguishable from "no backlog". The
manifest documents the self-hosted equivalents (`no_poller_tasks`, sync match
rate) inline.

Note also `sum()` and never `rate()` on `temporal_cloud_v1_*` metrics: they are
pre-computed per-second gauges.

### Verify it yourself

```bash
kubectl get scaledobject temporal-worker -n temporal-workers    # READY=True
kubectl get hpa -n temporal-workers                             # TARGETS must not be <unknown>
```

`<unknown>` means the Prometheus query returns nothing — usually a
`namespace`/`task_queue` label that does not match, or the **seconds vs
milliseconds** mistake (Go/Java emit seconds; TypeScript, Python and .NET emit
milliseconds, so the threshold is `0.2` or `200` and the metric name differs).

Then drive load and watch it act:

```bash
kubectl get hpa -n temporal-workers -w
```

Replicas should rise while schedule-to-start is high, and settle **slowly**
afterwards rather than snapping back — scale-up is immediate, scale-down uses a
10-minute stabilisation window, because dropping a Worker mid-Activity creates
the very latency the autoscaler exists to prevent.

### KEDA or a plain HPA — pick one, never both

`scaledobject.yaml` (KEDA) and `hpa.yaml` (plain HPA) do the same job with the
same signal. **Applying both makes them fight**: KEDA creates its own HPA for
the same Deployment, two controllers own `replicas`, and the fleet oscillates.

| | KEDA | Plain HPA |
|---|---|---|
| Reads Prometheus | directly | **only via the Prometheus Adapter** — an extra APIService in the control plane |
| Objects to manage | one CRD | HPA + adapter rule + APIService |
| Failure mode | ScaledObject `READY=False` | HPA sits at `<unknown>` and silently stops scaling |

Use KEDA unless you cannot install it. The plain HPA earns its place when your
platform team forbids new operators, or you already run the adapter.

Two details the HPA version gets wrong easily, both silent:

- **`AverageValue`, not `Value`.** `Value` ignores replica count, so the HPA has
  no model of what adding a pod does — it overshoots to `maxReplicas` and
  oscillates. `AverageValue` gives `desired = ceil(replicas × current / target)`.
- **A `metricSelector` scoping to one Task Queue.** Without it the adapter
  aggregates every queue into one number, and a healthy queue masks a starved one.

### Karpenter: consolidation has the same blind spot as CPU autoscaling

`k8s/karpenter/nodepool.yaml` is written against **`karpenter.sh/v1`** and
**`karpenter.k8s.aws/v1`**. Karpenter went v1 in August 2024; v1beta1 manifests
do not apply. Two changes break a copied v1beta1 file outright: `kubelet` config
moved from NodePool to EC2NodeClass, and `amiSelectorTerms` became **required**.

The reason this file needs care is the same inversion as the autoscaler:

> Consolidation removes **underutilized** nodes. A Worker holding a 20-minute
> Activity while waiting on a slow downstream API is, by every resource measure
> Karpenter can see, idle. It is the most expensive pod on the cluster to kill
> and it looks like the cheapest.

Killing it becomes an Activity timeout and a retry — so aggressive consolidation
manufactures the very backlog the autoscaler exists to prevent, during the quiet
periods when consolidation is most eager. Hence:

| Setting | Why |
|---|---|
| `consolidationPolicy: WhenEmpty` | Not the `WhenEmptyOrUnderutilized` default. Choose by Activity length — seconds-long Activities can use the default and enjoy the bin-packing. |
| `terminationGracePeriod: 1h` | **The most important field.** The ceiling on draining, including `do-not-disrupt` pods. Set it above your longest Activity `StartToCloseTimeout`. |
| `consolidateAfter: 5m` | Not zero. Deploys briefly empty a node; it should not be destroyed and re-provisioned. |
| `budgets` | Caps voluntary disruption at 20%, and blocks it entirely during business hours. Without a budget, a drift or `expireAfter` wave can cycle the whole fleet at once and leave the queue unpolled. |
| `on-demand` only | Spot suits Workers *if* Activities are short, idempotent and heartbeating. Otherwise each interruption costs a full `StartToCloseTimeout` stall, invisible as an error and visible only as latency. |
| `httpPutResponseHopLimit: 1` | Stops a container reaching IMDS and assuming the **node** role, which would bypass IRSA entirely. |

**`karpenter.sh/do-not-disrupt: "true"` on the Deployment is a trap.** No node
ever consolidates, the fleet never shrinks, and you pay peak capacity forever —
and it still does not survive `terminationGracePeriod`. Use the PodDisruptionBudget
(already in `worker-deployment.yaml`), or the annotation's *duration* form set by
the app around long Activities.

The Deployment carries a matching `toleration` + `nodeSelector` for this
NodePool. **Delete both if you are not running Karpenter** — an unsatisfiable
`nodeSelector` leaves every Worker Pending, the queue unpolled, and nothing in
Temporal reports an error.

### Logs on EKS

The Docker-socket log collection in `demo/` is compose-specific. On Kubernetes it
becomes an **Alloy DaemonSet** reading pod logs — same Loki, same label
discipline, different discovery.
