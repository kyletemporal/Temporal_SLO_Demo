# Self-hosted Temporal on EKS

A self-hosted Temporal Service on AWS: EKS for the server roles, RDS
PostgreSQL for persistence **and** visibility, and the `production/` rules and
dashboards from this repo wired in unchanged. One `terraform apply`, one script.

| | |
|---|---|
| Temporal Server | **1.28.4** (chart `temporalio/temporal` **0.67.0**, the newest whose appVersion is 1.28) |
| Persistence + visibility | RDS PostgreSQL **16**, two databases on one instance, TLS required and host-verified |
| History shards | **512** by default — permanent, see below |
| Monitoring | kube-prometheus-stack 92.3.1 + `production/prometheus` rules + `production/grafana` dashboards |
| Kubernetes | EKS 1.36, three `m7i.xlarge` nodes across three AZs |

This is the environment the rest of the repo kept pointing at — "use the Helm
chart for anything real" — and never provided. It exists to reproduce
self-hosted behaviour on infrastructure that looks like a customer's, not to be
a production platform. See [Verification status](#verification-status) before
relying on any of it.

---

## Run it

```bash
cd aws/eks-selfhosted/terraform
cp terraform.tfvars.example terraform.tfvars   # set admin_cidrs at minimum
terraform init
terraform apply                                # ~20 min, EKS is most of it

../scripts/deploy.sh                           # ~10 min
../scripts/smoke-test.sh                       # Workflow + Activity + visibility, end to end
```

Then:

```bash
kubectl -n temporal   port-forward svc/temporal-web       8080:8080   # Temporal UI
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80     # Grafana, folder "Temporal (production)"
kubectl -n temporal   port-forward svc/temporal-frontend  7233:7233   # SDKs / CLI from your laptop
```

Grafana's admin password is in `secret/grafana-admin` in `monitoring`; the
script prints the command.

Tear down — in this order, which the script enforces:

```bash
../scripts/teardown.sh
```

### From AWS CloudShell

You don't need anything installed locally. Open CloudShell in the target
region and run:

```bash
curl -fsSLO https://raw.githubusercontent.com/kyletemporal/Temporal_SLO_Demo/main/aws/eks-selfhosted/scripts/cloudshell.sh
bash cloudshell.sh up
```

`up` does the following:
1. Installs pinned, checksum-verified Terraform, Helm and kubectl into
   `~/.local/bin`.
2. Shallow-clones this repo.
3. Creates a versioned, encrypted, private S3 bucket for Terraform state.
4. Runs `terraform apply`, which shows the plan and asks before changing
   anything.
5. Runs `deploy.sh` and then `smoke-test.sh`.

Other commands:

| | |
|---|---|
| `bash cloudshell.sh deploy` | Re-run only the Helm layer |
| `bash cloudshell.sh smoke` | End-to-end smoke test |
| `bash cloudshell.sh status` | Pods and releases |
| `bash cloudshell.sh connect` | Refresh kubeconfig and this session's IP on the allowlist |
| `bash cloudshell.sh down` | Tear it all down (asks for confirmation) |

Four CloudShell constraints shape the script:

- **Idle sessions end after ~20 minutes, and EKS takes about that long to
  create.** Keep the tab active. If the session drops anyway, open a new one
  and run the same command. State is in S3, so resources that finished
  creating are kept. A kill mid-apply can leave two things that a plain re-run
  doesn't fix: a stale state lock, and a resource AWS created but state never
  recorded. On failure the script prints the `force-unlock` and `import`
  commands for both. RDS uses a fixed identifier, so it can't silently create
  a second, orphaned instance.
- **`$HOME` is capped at 1 GB.** Terraform's working directory is about 880 MB
  (measured), so it goes in `/tmp` and is re-initialised each session. The
  tools and the repo use about 285 MB of home.
- **The public IP changes every session.** Every command *replaces* the EKS
  API allowlist with the current session's /32 plus `EXTRA_ADMIN_CIDRS`
  before touching the cluster, so earlier sessions' IPs drop off. To use
  kubectl from your laptop as well, add
  `EXTRA_ADMIN_CIDRS=<laptop-ip>/32`. CloudShell can't open the UI or
  Grafana, so port-forward from the laptop.
- **Your console identity becomes cluster admin.** It also needs permission
  to create VPC, EKS, RDS, IAM, Secrets Manager and S3 resources.

`REPO_URL` and `REPO_REF` override where the script clones from, for example
after the repository moves.

---

## Design decisions

### Terraform for AWS, Helm for Kubernetes — two layers, not one apply

`terraform/` creates the VPC, EKS and RDS and stops. It contains no
`helm_release` and no `kubernetes_*` resources. `scripts/deploy.sh` installs
the charts.

Putting both in one apply means configuring the Helm and Kubernetes providers
from the outputs of a cluster that does not exist yet on the first plan.
HashiCorp's own provider documentation warns against it: it works on day one,
then a cluster replacement or a credential expiry produces errors that look
like auth failures and are really ordering failures. It also couples two
things that change at different rates — a Temporal patch upgrade should not
need a `terraform apply` against the VPC.

The Helm values are plain files, so the same `values-temporal.yaml` can be
handed to Argo CD or Flux later without rewriting anything.

### PostgreSQL for visibility

Both stores live on one RDS instance, in two databases (`temporal`,
`temporal_visibility`). SQL advanced visibility needs PostgreSQL 12+ and
server 1.20+, both met.

The trade-off, stated plainly: Temporal recommends Elasticsearch/OpenSearch
for visibility on any cluster that "spawns more than a few Workflow
Executions". On Postgres, heavy List/Count traffic competes with history
writes for the same instance. If what you are reproducing involves visibility
load, that contention is either the thing under test or a confound — and if
it is a confound, move visibility to its own instance.

### 512 history shards

`numHistoryShards` is fixed when the cluster first starts and **can never
change**. Temporal's guidance is 512 for small production clusters, with large
clusters rarely going beyond 4,096, and the right number comes from load
testing rather than from a table. Too few, and shard-lock contention caps
throughput however many history pods you run. Too many, and every shard's
queues cost CPU, memory and database polling.

Set it with `NUM_HISTORY_SHARDS` on the **first** deploy. `deploy.sh` reads the
value an existing cluster was created with and refuses to deploy a different
one, rather than letting the change look like it took effect.

If this environment is reproducing someone else's cluster, use **their** shard
count. It changes how load spreads across history pods, so a repro at a
different count is a different system.

### Server 1.28 on a 1.28 chart

Images are pinned to 1.28.4 on chart 0.67.0, whose appVersion is 1.28.1. A
newer chart with an older image looks equivalent and is not: the chart renders
the server's config file, and a template written for 1.3x can emit keys 1.28
rejects or ignores.

### Credentials never pass through Terraform state

- The **RDS master password** is managed by RDS in Secrets Manager
  (`manage_master_user_password`). It is used once per deploy, by a bootstrap
  Job that exists only for the length of that Job.
- The **`temporal` role** is least-privilege: LOGIN, owner of its two
  databases, no `CREATEDB`/`CREATEROLE`. `deploy.sh` generates its password and
  writes it straight to a Secrets Manager secret that Terraform created empty.
- **Grafana's admin password** is generated into a Kubernetes Secret, never
  the chart's default.

This is SECURITY.md P4 (secrets in Terraform state) designed out, not
documented around.

### TLS to the database is required, not optional

`rds.force_ssl = 1` in the parameter group, and Temporal connects with host
verification against the RDS CA bundle. If anything quietly fell back to
plaintext, it would now fail loudly.

### The `namespace` label collision

Prometheus Operator puts the Kubernetes namespace on every target as
`namespace`. Temporal's server metrics already have a `namespace` label: the
Temporal Namespace. By default the target label wins, and Temporal's is
renamed `exported_namespace`. Every per-namespace rule in
`production/prometheus` then reports the Kubernetes namespace on every series.
Nothing errors; every answer is wrong.

The chart's ServiceMonitors cannot fix that, so `helm/servicemonitor.yaml`
replaces them. It moves the Kubernetes value to `k8s_namespace` and leaves
`namespace` to Temporal. Measured on the verification run: 0 series with
`exported_namespace`, and `namespace` holds `default` and `temporal_system`.

---

## Reaching the frontend

The frontend is `ClusterIP` only. Workers inside the cluster use
`temporal-frontend.temporal.svc:7233`, and from a laptop you port-forward.

To reach it from elsewhere in the VPC, expose it through an **internal** NLB.
Set this in a values override:

```yaml
server:
  frontend:
    service:
      type: LoadBalancer
      annotations:
        service.beta.kubernetes.io/aws-load-balancer-scheme: internal
        service.beta.kubernetes.io/aws-load-balancer-type: nlb
```

Before anything outside the VPC connects, configure frontend mTLS
(`server.config.tls` in the chart). Without it, the frontend accepts any
client that can route to it.

---

## Cost

These are approximate us-east-1 on-demand list prices. Check them against the
AWS pricing calculator before you rely on them.

| | Hourly | Monthly |
|---|---|---|
| EKS control plane | $0.10 | ~$73 |
| 3 × m7i.xlarge nodes | ~$0.60 | ~$440 |
| RDS db.m7g.large, single-AZ (Multi-AZ doubles it) | ~$0.17 | ~$125 |
| RDS 100 GB gp3, plus node and Prometheus EBS | — | ~$30 |
| One NAT gateway, plus data processed | ~$0.05 | ~$35+ |
| **Total** | **~$0.95** | **~$700** |

What `teardown.sh` deliberately leaves behind, and which keeps billing until
you delete it:
- the final RDS snapshot
- RDS automated backups
- the two Secrets Manager secrets, for their 7-day recovery window

`teardown.sh` waits for the Prometheus volume to be released before it
destroys the cluster, and stops if it isn't. Destroying first would orphan a
billable EBS volume. `FORCE_DESTROY=1` overrides that check.

---

## Verification status

**What has run:**

- **Terraform:** `validate` passes against AWS provider 6.68.0 and modules
  eks 21.29.0, vpc 6.7.3, and eks-pod-identity 2.9.0. `terraform test`
  (`tests/plan.tftest.hcl`) plans the whole module graph against a mocked AWS
  provider. It asserts: PostgreSQL 16, managed master password, private and
  encrypted RDS, `force_ssl`, and deletion protection on. It also confirms
  `admin_cidrs` rejects `0.0.0.0/0` and an empty list.
- **Helm layer, end to end, on a local kind cluster:** `deploy.sh` ran
  unmodified in env mode against Postgres 16, which was configured like RDS:
  - TLS-only `pg_hba` (plaintext rejected — checked)
  - a CA-signed certificate verified by hostname
  - a **non-superuser** admin role, so the bootstrap's
    `GRANT temporal TO CURRENT_USER` path was exercised

  Results:
  - the bootstrap Job created the role and both databases
  - the schema Job migrated both stores
  - all six Deployments rolled out
  - the `default` namespace was registered
  - `smoke-test.sh` passed: a Workflow and Activity completed, and the closed
    execution appeared in visibility
  - all four server roles were scraped
  - all **65** `production/` rules evaluated healthy, and **41 of 43**
    recording rules returned data
  - all four `production/` dashboards were provisioned in Grafana
  - a re-run was idempotent (schema revision 2, credentials reused, smoke test
    still passing)
  - `NUM_HISTORY_SHARDS=1024` on the 512-shard cluster was refused before
    anything changed

  The kind run used smaller replicas and resource requests so that it fit on
  one node. Every other value was the same file.
- **Scripts:** shellcheck is clean. `cloudshell.sh` was run in an
  `amazonlinux:2023` linux/amd64 container (CloudShell's base). In that run,
  tool install and checksum checks, the repo clone (including the
  unmerged-branch fallback), tfvars and backend generation, and
  `terraform init`/`validate`/`test` all ran. The S3, STS and EKS calls did
  not.

**What has not run** (it needs an AWS account):

- `terraform apply`
- the EBS CSI driver and gp3 volumes
- the RDS CA download and verify-full against a real RDS hostname
- the Secrets Manager reads and writes in `deploy.sh`
- the security-group path from pods to RDS
- `teardown.sh`

None of these is exotic. "Not exotic" is still not "verified", so treat the
first real apply as the test.

**The two empty recording rules** are `slo:workflow_compliance:ratio` and
`slo:workflow_budget_remaining:ratio`. They read `temporal_slo_*` series from
the `monitor/` Visibility service, which this environment does not deploy yet.
Running `monitor/` in the cluster is the next step, and it is what makes the
stuck-Workflow SLO work here.
