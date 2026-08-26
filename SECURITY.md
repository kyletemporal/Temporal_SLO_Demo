# Security

Risk assessment for this repository, written from a DevSecOps standpoint, plus
the process for reporting a vulnerability.

Assessed 2026-08-18 against commit `d5beedf` and the working tree on top of it.
Scans are reproducible — see [Re-running the scans](#re-running-the-scans).

---

## 1. What this repository is, and the one assumption that makes it dangerous

This repo contains two things with **opposite** risk postures, and almost every
finding below comes from confusing them:

| | What it is | Intended exposure |
|---|---|---|
| `demo/` | A self-contained lab. Docker Compose, seeded data, chaos scenarios. | **One laptop, one operator, one demo.** Not a deployment template. |
| `production/`, `cloud/`, `app-team/`, `monitor/`, `terraform/`, `aws/` | Rules, dashboards, modules and a service intended to run against real clusters. | Real environments, real data. |

**The load-bearing assumption is that nobody copies `demo/` into an
environment.** It authenticates nobody, encrypts nothing, and publishes
fourteen ports. That is a reasonable set of choices for a lab that lives for
the length of a demo, and an unreasonable one for anything else.

Where a finding below says *demo-only*, it means the risk is acceptable **for a
lab on a trusted network** and unacceptable anywhere else. It does not mean
"ignore it" — laptops sit on conference wifi, corporate VPNs and hotel
networks, and `docker compose up` outlives the meeting.

### Threat model

In scope:

- An attacker on the same network as a running `demo/` stack.
- A contributor who commits a secret by accident.
- A customer who adapts these manifests and modules for production.
- Supply-chain risk in the images and Go modules this repo pulls.

Out of scope: the security of Temporal Server itself, of AWS, or of Grafana —
upstream products with their own advisories. Multi-tenant hardening of the demo
is out of scope by design; it is single-tenant by construction.

### Severity

| | Meaning |
|---|---|
| **Critical** | Remote compromise of the host or of customer data, no credentials needed. |
| **High** | Full control of the workload, host escape from a container, or credential disclosure. |
| **Medium** | Unauthenticated access to telemetry and business identifiers, or a control that is missing/opt-out where it should be default. |
| **Low** | Defence-in-depth gap. Needs another failure to matter. |

---

## 2. Risk register

| # | Severity | Area | Finding | Fixed? |
|---|---|---|---|---|
| [D1](#d1) | **High** | `demo/` | Docker socket mounted into the Alloy container — `:ro` does not make the Docker API read-only | Open, by design |
| [D2](#d2) | **High** | `demo/` | 14 ports published on all interfaces; Temporal Frontend gRPC and the order API are unauthenticated | Open, by design |
| [D3](#d3) | **Medium** | repo | `.env` is not in `.gitignore`, while `.env.example` invites creating one | **Fixed** |
| [D4](#d4) | **Medium** | `demo/` | Anonymous Grafana can run arbitrary queries against every datasource | Open, by design |
| [D5](#d5) | **Medium** | `demo/` | Default credentials (`admin`/`admin`, Postgres `temporal`) | Open, by design |
| [D6](#d6) | **Medium** | `demo/app` | `/orders` has no auth, no body limit, attacker-controlled Workflow ID, and can disable the execution timeout | Open, partly by design |
| [D11](#d11) | **Medium** | `demo/` | Nexus callbacks accept any host over cleartext (`Pattern: "*"`, `AllowInsecure: true`) | Open, by design |
| [P1](#p1) | **Medium** | `monitor/` | Temporal client supports neither TLS nor credentials; image has no CA bundle | Open |
| [P2](#p2) | **Medium** | repo | `.terraform.lock.hcl` is gitignored, discarding provider checksum pinning | Open |
| [P3](#p3) | **Medium** | repo | `*.tfvars` is not gitignored at the root; and the ignore rule that did exist silently dropped two files a pattern depends on | **Fixed** |
| [P4](#p4) | **Medium** | `terraform/` | Terraform state stores Temporal API key tokens in plaintext | Open, documented |
| [P5](#p5) | **Medium** | `aws/` | `external_id` (confused-deputy protection) is optional | Open, documented |
| [P8](#p8) | **Medium** | Go | 6 reachable Go standard-library vulnerabilities | Open, upstream |
| [D7](#d7) | Low | `demo/app`, `monitor/` | No `WriteTimeout`/`IdleTimeout` on HTTP servers | Open |
| [D8](#d8) | Low | `demo/app` | pprof binds `0.0.0.0` inside the container | Open |
| [D9](#d9) | Low | `demo/` | OTLP export is plaintext (`insecure: true`) | Open, by design |
| [D10](#d10) | Low | `demo/` | Images pinned by tag, not by digest | Open, deliberate |
| [P6](#p6) | Low | `aws/` | No bucket policy denying non-TLS access; no access logging | Open |
| [P7](#p7) | Low | `aws/k8s` | No `seccompProfile`, no NetworkPolicy, no writable `/tmp` | Open |

No Critical findings. No secrets are committed to this repository — every
credential-shaped string is either a documented demo default or a variable.

---

## 3. Findings

### <a id="d1"></a>D1 — Docker socket in the Alloy container · **High** · demo-only

`demo/docker-compose.yml:262`

```yaml
- /var/run/docker.sock:/var/run/docker.sock:ro
```

Alloy uses the socket twice — `discovery.docker` to find containers and
`loki.source.docker` to read their logs (`demo/alloy/config.alloy:8,47`).

**`:ro` is misleading here.** It makes the socket *file* read-only; it does not
make the Docker *API* read-only. Anything that can talk to that socket can still
`POST /containers/create` with `Privileged: true` and a host bind mount. So this
is a **container-escape-to-root-on-host** primitive granted to a third-party
image, and Alloy's own UI is published on port 12345 (D2).

It also reads the logs of every container in the project, and Docker's log API
returns environment-derived output — so it is a credential-adjacent path too.

**Accepted for the lab** because Docker-socket discovery is genuinely the
simplest way to collect Compose logs, and the README already states this is the
Compose-specific approach. **It must not survive into a cluster.** On
Kubernetes the equivalent is an Alloy DaemonSet reading pod logs from
`/var/log/pods` with a read-only host path and a dedicated ServiceAccount — no
container runtime socket. `aws/README.md` says this; it is repeated here
because this is the finding most likely to be copied by accident.

If you want the lab itself hardened, put a socket proxy
(`tecnativa/docker-socket-proxy`) in front of Alloy and allow only
`CONTAINERS=1`, `POST=0`.

---

### <a id="d2"></a>D2 — Everything is published, most of it unauthenticated · **High** · demo-only

Fourteen ports are bound to all interfaces (Docker's default for `"host:container"`):

| Port | Service | Auth |
|---|---|---|
| **7233** | **Temporal Frontend gRPC** | **none** |
| **7243** | **Temporal Frontend HTTP** (Nexus callbacks) | **none** |
| **8081** | **Order API** | **none** |
| 8080 | Temporal Web UI | none |
| 3000 | Grafana | anonymous viewer |
| 9090 / 3100 / 3200 / 4040 | Prometheus / Loki / Tempo / Pyroscope | none |
| 4317 / 4318 | OTLP ingest | none |
| 12345 | Alloy UI | none |
| 8000 / 9111 | Temporal + monitor metrics | none |

Port 7233 is the serious one: the Frontend gRPC API is the full Temporal control
plane. Anyone who can route to it can start, signal, terminate and read the
history of every Workflow — that is, read your business payloads. Port 8081 lets
them create Workflows without even that much.

**Mitigation for the lab**, if it will ever be on an untrusted network — bind to
loopback only, which costs nothing because every consumer is either in the
Compose network or on the host:

```yaml
ports:
  - "127.0.0.1:7233:7233"
```

Do this for all fourteen. Keep 3000 on `0.0.0.0` only when you are screen-sharing
to a room that needs to reach it, and stop the stack afterwards
(`make down`) rather than leaving it up.

---

### <a id="d3"></a>D3 — `.env` was not gitignored · **Medium** · **fixed**

`demo/.env.example` exists and the Compose header refers to "or edit `.env`", so
the intended workflow creates `demo/.env`. That path matched no `.gitignore`
rule, so a user who adds `TEMPORAL_API_KEY` or a Grafana password to it — the
natural next step when pointing the stack at a real cluster — could commit it.

The shipped `.env.example` contains only image version pins, so nothing is
leaked today. The exposure is created the moment a customer adapts it, which is
exactly what this repo asks them to do.

**Fixed** — `.env` and `.env.*` (except `.env.example`) are now ignored at any
depth.

---

### <a id="d4"></a>D4 — Anonymous Grafana can query every datasource · **Medium** · demo-only

`demo/docker-compose.yml` runs Grafana with `GF_AUTH_ANONYMOUS_ENABLED=true`,
and this assessment's own session added `GF_USERS_VIEWERS_CAN_EDIT=true`.

**That second flag is disclosed here because it widens access on purpose.**
Grafana grants `datasources:explore` to Editor and above; without it, an
anonymous viewer opening `/explore` is silently redirected to the home page,
which broke every trace button and every trace→logs link on the boards. Adding
it restores Explore — and Explore is a general-purpose query console.

So an unauthenticated visitor can run arbitrary PromQL, LogQL and TraceQL. The
consequence is not "they can see charts", it is:

- **Loki holds raw log lines with no redaction.** `demo/alloy/config.alloy`
  extracts a level and a service and ships the rest of the line verbatim. The
  golden-signals board deliberately surfaces WorkflowIDs and RunIDs, because
  those are the only route from "something is stuck" to an execution. Anything
  the application logs is readable.
- **Tempo holds spans.** Payload attributes *are* redacted at the collector
  (`demo/otel/collector.yaml`, `attributes/redact` drops `temporalPayloads`,
  `temporalWorkflowInput`, `temporalWorkflowResult`) — a good control, and worth
  noting that **logs have no equivalent**.

Combined with D2 this means: reach the laptop, read the business identifiers.

**Mitigation** — for anything beyond a local demo, turn anonymous off and let
Explore follow from a real login:

```yaml
GF_AUTH_ANONYMOUS_ENABLED: "false"
# GF_USERS_VIEWERS_CAN_EDIT is then unnecessary — logged-in Editors have Explore.
```

`scripts/validate.sh` §9 checks the permission is present, so if you disable
anonymous access that check will report the trace links as unreachable **for
anonymous users** — which is then correct, not a regression.

---

### <a id="d5"></a>D5 — Default credentials · **Medium** · demo-only

`GF_SECURITY_ADMIN_USER=admin` / `GF_SECURITY_ADMIN_PASSWORD=admin`
(`docker-compose.yml:173`) and `POSTGRES_PASSWORD=temporal` (`:24`).

Appropriate for a lab, and both are overridable via environment. The risk is
adaptation: a customer who copies the Compose file to a shared host inherits
`admin`/`admin` on a Grafana that is now reachable. Postgres is not published,
so it is reachable only from the Compose network.

**Mitigation** — override both from a secret manager or `.env` (now ignored,
per D3) before the stack leaves your machine.

---

### <a id="d6"></a>D6 — The order API accepts anything from anyone · **Medium**

`demo/app/api.go:68-146`. Four distinct issues, in descending order:

1. **No request body limit.** `json.NewDecoder(r.Body).Decode(&req)` reads
   without an `http.MaxBytesReader`, so a large POST is a memory-exhaustion DoS
   against an unauthenticated endpoint.
2. **`OrderID` is attacker-controlled and unvalidated**, concatenated straight
   into the Workflow ID (`workflowIDPrefix + req.OrderID`, line 89). No length
   cap, no charset restriction. A caller chooses Workflow IDs — enough to
   collide with, or deliberately deduplicate against, legitimate executions.
3. **`StuckMode` disables the execution timeout** (line 113-115), remotely, with
   no authentication. Each such call creates a Workflow that never ends. This is
   deliberate — it is how `chaos-stuck` demonstrates the case no metric can see,
   and the comment explains it well — but it is a remotely-triggerable unbounded
   resource leak, and it is reachable from the network per D2.
4. **`TaskQueue` is attacker-controlled** (line 80), so a caller can enqueue to
   any queue, including one nobody polls.

Errors are also returned to the caller verbatim (`err.Error()`, line 125),
disclosing internal addresses and Temporal error detail.

**This is demo code and should stay simple.** The one change worth making
regardless is the body limit, which costs a line and removes the only issue
here that is a straightforward DoS:

```go
r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
```

Do not copy this handler into a service. If you adapt it: authenticate it,
validate `OrderID` against `^[A-Za-z0-9_-]{1,64}$`, allow-list task queues, and
gate `StuckMode` behind a build tag or an env flag that is off by default.

---

### <a id="d11"></a>D11 — Nexus callbacks accept any host, in cleartext · **Medium** · demo-only

`demo/temporal/dynamicconfig/development.yaml`

```yaml
component.callbacks.allowedAddresses:
  - value:
      - Pattern: "*"
        AllowInsecure: true
```

This is the allow-list the server checks before accepting a Nexus Operation
callback URL. As set, it is the most permissive configuration available:

- **`Pattern: "*"`** accepts a callback to *any* host. Combined with the ability
  to register an Endpoint — which on this stack is unauthenticated, per D2 —
  it is a server-side request forgery primitive: the cluster will issue requests
  to a destination of the caller's choosing.
- **`AllowInsecure: true`** accepts plaintext HTTP, so Operation results —
  business payloads — travel unencrypted and are open to interception and
  tampering.

Temporal's own documentation restricts both to development, and this repo's
cluster terminates no TLS, so `AllowInsecure: false` would break the demo rather
than secure it. It is set this way knowingly.

**Mitigation** — for anything beyond a laptop, narrow the pattern to hosts you
control and turn insecure callbacks off once TLS terminates in front of the
frontend:

```yaml
component.callbacks.allowedAddresses:
  - value:
      - Pattern: "*.example.com"
        AllowInsecure: false
```

On Server 1.30.X and later this key is superseded by
`component.nexusoperations.useSystemCallbackURL: true`, which removes the
hand-written URL and its allow-list from the picture entirely — the better
answer where the version allows it. See `demo/docs/NEXUS.md`.

---

### <a id="p1"></a>P1 — The monitor cannot talk to a secured cluster · **Medium**

`monitor/cmd/monitor/main.go:64`:

```go
c, err := client.Dial(client.Options{
    HostPort:  cfg.Deployment.Address,
    Namespace: cfg.Deployment.Namespace,
    Logger:    log,
})
```

No `ConnectionOptions.TLS`, no `Credentials`. And `monitor/Dockerfile` builds on
bare `alpine:3.21` **without `ca-certificates`** — so even outbound TLS would
fail certificate verification (`demo/app/Dockerfile` installs the CA bundle;
this one does not).

This service is documented as production-facing, and `terraform/` exists to
provision Temporal Cloud namespaces. As shipped the monitor can only reach a
plaintext, unauthenticated Frontend. The security concern is not the missing
feature, it is the workaround it invites: an operator who needs this working
against a real cluster will reach for a plaintext tunnel or an
`InsecureSkipVerify` patch.

**Fix** — add `ca-certificates` to the runtime image, and plumb API-key and
mTLS options through `Deployment` config. Until then, treat the monitor as
same-network-only and say so in its runbook.

---

### <a id="p2"></a>P2 — Provider lock files are gitignored · **Medium**

`.gitignore:` `**/.terraform.lock.hcl`

`.terraform.lock.hcl` is the *opposite* of a build artefact: it records the
**checksums** of every provider, so `terraform init` fails if a published
provider version is ever republished with different bytes. Ignoring it discards
that supply-chain guarantee, and every `init` re-resolves whatever the registry
currently serves.

This was almost certainly collateral from the fix for the 776 MB provider binary
that got committed (the comment in `.gitignore` records that incident). The
directory `**/.terraform/` is the artefact and must stay ignored; the lock file
should be committed.

**Fix** — remove the `**/.terraform.lock.hcl` line and commit the lock files.

---

### <a id="p3"></a>P3 — `*.tfvars` was not gitignored · **Medium** · **fixed**

`.tfvars` is where Terraform users conventionally put values that vary per
environment, which in practice is where secrets land.

Investigating this turned up a **pre-existing bug** rather than a leak. The two
files at `terraform/patterns/01-environment-promotion/envs/{dev,prod}.tfvars`
exist on disk but were **never committed** — `terraform/.gitignore` had been
swallowing them. Meanwhile that pattern's `main.tf:11` tells you to run:

```
terraform apply -var-file=envs/prod.tfvars
```

So a fresh clone got a pattern whose own documented command fails on a missing
file, and whose entire point — dev and prod differing in *data* rather than in a
forked configuration — was invisible. Both files are benign: environment names,
regions, search attributes and tags, no credentials.

**Fixed** — `*.tfvars`, `*.auto.tfvars` and `*.tfvars.json` are now ignored,
with those two files re-included by explicit negation. The negation had to go in
`terraform/.gitignore` rather than the root one: a nested `.gitignore` takes
precedence over a parent, and `terraform/.gitignore` already carried its own
`*.tfvars` rule, so a root-level negation was silently overridden.

Note the consequence: adding a *third* environment to that pattern
(`envs/staging.tfvars`) will be ignored by default. That is the safe direction
to fail, but `git add -f` is then required deliberately.

---

### <a id="p4"></a>P4 — API key tokens live in Terraform state · **Medium** · documented

`temporalcloud_apikey.token` is a real credential, and Terraform writes resource
attributes to state in plaintext. `terraform/patterns/04-split-ownership/platform/main.tf`
already says so and recommends `encrypt = true` on the S3 backend, and
`terraform/patterns/03-credential-rotation` is built around the problem. Good.

Restated here because it is the highest-value target in the whole repo: **state
access is credential access.** Encrypt the backend, restrict `s3:GetObject` on
the state bucket to the CI role, enable state locking, and never run these
configurations with a personal account that also has read access to the bucket.

---

### <a id="p5"></a>P5 — Confused-deputy protection is opt-out · **Medium**

`aws/terraform/export-sink/main.tf` adds the `sts:ExternalId` condition only
when `var.external_id != null`, and the variable has no default. The
documentation calls it "strongly recommended".

Temporal Cloud assumes your role from **its** account, using intermediary roles
shared across customers. Without an ExternalId, the trust policy says "any
principal in that set may assume this role" — the textbook confused-deputy
setup, where knowing your role ARN is most of the attack.

The rest of this module is well built: public access blocked, SSE enabled,
versioning on, `prevent_destroy`, least-privilege write policy scoped to the one
bucket, KMS grants conditional. This is the one gap.

**Fix** — make `external_id` required (drop the null default), or add a
`precondition` that fails the plan when it is unset.

---

### <a id="p8"></a>P8 — Go standard-library vulnerabilities · **Medium** · upstream

`govulncheck` against the toolchain the images actually build with
(`golang:1.25-alpine` → go1.25.12):

| Module | Reachable | IDs |
|---|---|---|
| `demo/app` | **6** | GO-2026-6218, GO-2026-6091, GO-2026-6090, GO-2026-6089, GO-2026-5972, GO-2026-5026 |
| `monitor` | **4** | GO-2026-6091, GO-2026-6090, GO-2026-6089, GO-2026-5972 |

Every reachable finding is in the Go standard library, and **every one is fixed
in go1.25.13**. Nothing is reachable in this repository's own code or in any
third-party module (a further 1-2 per module exist in required modules but are
not called).

Most relevant to what this repo does: GO-2026-6089 (`ReadHeaderTimeout` not
applied during the unencrypted HTTP/2 check) touches the metrics and pprof
listeners directly, and GO-2026-6218 (quadratic `net/url` path resolution) is
reachable from any HTTP handler.

**Fix** — rebuild. `golang:1.25-alpine` is a floating tag, so
`docker compose build --no-cache` picks up 1.25.13 as soon as it is published,
with no file change. Verify with the scan command in §5. This is also the
counter-argument to pinning that base image by digest (D10) — see the trade-off
recorded there.

---

### Low-severity findings

<a id="d7"></a>**D7 — Missing HTTP server timeouts.** `demo/app/api.go:151`,
`demo/app/profiling.go:45` and `monitor/cmd/monitor/main.go:91` set
`ReadHeaderTimeout` but no `WriteTimeout` or `IdleTimeout`. Slow readers hold
connections and goroutines indefinitely. `ReadHeaderTimeout` — the one that
blocks Slowloris — is present, which is why this is Low. Add `WriteTimeout:
30s`, `IdleTimeout: 120s`.

<a id="d8"></a>**D8 — pprof binds `0.0.0.0` in-container.**
`demo/app/api.go:36` and `worker.go:29` call
`startPprofServer("0.0.0.0:" + env("PPROF_PORT", "6060"))`. Port 6060 is
correctly **not** published, and the code comments say so — but any container on
the `tobs` network can still reach it, and heap profiles can contain live
payload data. Bind `127.0.0.1:6060`; `docker compose exec` still works.

<a id="d9"></a>**D9 — Plaintext OTLP.** `demo/otel/collector.yaml:90` sets
`tls.insecure: true` for the Tempo exporter. Compose-network-internal, so
demo-appropriate. Enable TLS when the collector and backend are on different
hosts.

<a id="d10"></a>**D10 — Images pinned by tag, not digest.** All eleven images use
`${VAR:-version}` tags. Tags are mutable, so a rebuild can silently pull
different bytes. Deliberate: it is also what lets a rebuild pick up the Go
1.25.13 fix in P8 without a code change. Digest-pin for production, keep tags
for the lab, and if you digest-pin add a scanner to tell you when to move.

<a id="p6"></a>**P6 — S3 controls not fully closed.** `aws/terraform/export-sink`
has no bucket policy denying `aws:SecureTransport: false`, and no server access
logging. Both are standard CIS controls for a bucket holding audit data.

<a id="p7"></a>**P7 — Kubernetes hardening gaps.** `aws/k8s/worker/worker-deployment.yaml`
sets `runAsNonRoot`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`
and `drop: ["ALL"]` — good. Missing: `seccompProfile: {type: RuntimeDefault}`,
any NetworkPolicy restricting egress to Temporal and the collector, and a
writable `emptyDir` at `/tmp` (with `readOnlyRootFilesystem: true`, a Go process
that needs scratch space will fail at runtime, and the tempting "fix" is to
remove the read-only flag).

---

## 4. Hardening checklist before this touches an environment

Ordered by consequence, not effort.

- [ ] **Do not deploy `demo/`.** Take the rules, dashboards and `monitor/`; leave the Compose file.
- [ ] Remove the Docker socket mount; use an Alloy DaemonSet reading pod logs (D1).
- [ ] Bind every published port to `127.0.0.1`, or run the stack on an isolated network (D2).
- [ ] Turn off `GF_AUTH_ANONYMOUS_ENABLED`; put Grafana behind your IdP (D4).
- [ ] Replace `admin`/`admin` and the Postgres password from a secret manager (D5).
- [ ] Enable TLS and authentication on the Temporal Frontend. Nothing here does it for you (D2).
- [ ] Add TLS + credentials to the monitor, and `ca-certificates` to its image (P1).
- [ ] Commit `.terraform.lock.hcl`; keep `.terraform/` ignored (P2).
- [ ] Encrypt and lock the Terraform state backend; restrict who can read it (P4).
- [ ] Make `external_id` required on the export sink (P5).
- [ ] Narrow the Nexus callback allow-list and disable insecure callbacks (D11).
- [ ] Rebuild images to pick up go1.25.13 (P8), and re-run the scan.
- [ ] Decide your redaction policy for **logs** before shipping them to Loki. The collector redacts span payloads; nothing redacts log lines (D4).

---

## 5. Re-running the scans

Everything in §3 is reproducible. No proprietary tooling.

**Go vulnerabilities**, against the toolchain the images actually use — not your
local Go, which will report a different stdlib version:

```bash
docker run --rm -v "$PWD":/src -w /src golang:1.25-alpine sh -c '
  apk add --no-cache git >/dev/null
  go install golang.org/x/vuln/cmd/govulncheck@latest >/dev/null
  for m in demo/app monitor; do echo "== $m"; (cd $m && /go/bin/govulncheck ./...); done'
```

**Committed secrets** — history as well as the working tree:

```bash
docker run --rm -v "$PWD":/repo zricethezav/gitleaks:latest detect --source=/repo --no-git
docker run --rm -v "$PWD":/repo zricethezav/gitleaks:latest detect --source=/repo
```

**Container images:**

```bash
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy:latest image temporal-obs-demo-api:latest
```

**Terraform:**

```bash
docker run --rm -v "$PWD":/src aquasec/tfsec:latest /src/terraform /src/aws/terraform
docker run --rm -v "$PWD":/src bridgecrew/checkov:latest -d /src/aws/terraform
```

**Kubernetes manifests:**

```bash
docker run --rm -v "$PWD":/src zegl/kube-score:latest score /src/aws/k8s/worker/*.yaml
```

**The lab's own checks** — `scripts/validate.sh` §9 verifies the Grafana Explore
permission and that every trace deep link resolves, which is the check that
catches D4 being changed in either direction:

```bash
cd demo && bash scripts/validate.sh
```

---

## 6. Reporting a vulnerability

This is a **community** repository. It is not a Temporal product, it is not
supported by Temporal, and it carries no security SLA — see `LICENSE` and the
statement in `README.md`.

- **In this repository:** open a GitHub issue, or contact the maintainer
  directly for anything you would not want in a public issue. Please include the
  commit, the file and a reproduction.
- **In Temporal itself** (Server, SDKs, Cloud): report to Temporal via
  <https://temporal.io/security>, not here.
- **In an upstream component** (Grafana, Loki, Tempo, Alloy, Pyroscope,
  OpenTelemetry Collector, Postgres): report upstream. If it also needs a change
  here — a version bump, a configuration default — an issue here is welcome.

No bounty is offered. Findings will be acknowledged in the risk register above.
