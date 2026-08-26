# Nexus and Namespace management, self-hosted

How to enable Nexus on this stack, how to create and manage Namespaces and
Endpoints, and the two things that make self-hosted Nexus look configured when
it is not.

Built and verified against **Temporal Server 1.27.4** (`temporalio/auto-setup`),
following <https://docs.temporal.io/production-deployment/self-hosted-guide/nexus>.

---

## What Nexus is for

A Workflow in one Namespace calls a service exposed by another Namespace,
through a named **Endpoint**, without knowing where the handler runs.

The point is the indirection. Callers reference `payments-api`; the platform
team can move the handler between Task Queues or Namespaces by updating the
Endpoint, and no caller is redeployed. It is how you let two teams integrate
without either sharing a Namespace — which is the usual alternative, and which
couples their retention, their quotas and their blast radius.

---

## The state of a stock 1.27.4 cluster

Three quarters of the setup is already done, which is worth knowing before you
start changing configuration that is already correct:

| | Status on stock `auto-setup:1.27.4` |
|---|---|
| Nexus APIs | **On by default.** `temporal operator nexus endpoint list` answers. There is no `system.enableNexus` to set. |
| Frontend HTTP port | **Already 7243.** The config template renders `httpPort: 7243` and `httpAddress: 127.0.0.1:7243` with no env var set. `FRONTEND_HTTP_PORT` overrides it. |
| Publishing 7243 | **Not done** — this repo adds it to `docker-compose.yml`. |
| Callback configuration | **Not done** — this repo adds `temporal/dynamicconfig/development.yaml`. |

### The failure mode that matters

**Registering an Endpoint succeeds with no callback configuration at all.**

You can create Endpoints, list them, point them at Namespaces, and get clean
success messages on a cluster where Nexus cannot actually complete a single
Operation. The callback config is only consulted when a Workflow *invokes* an
Operation — so the break surfaces at first real use, typically well after
whoever set it up has moved on and concluded it works.

That is what `make nexus-doctor` exists for. It checks the parts that
registration does not.

```
$ make nexus-doctor

== 1. Reaching the server
    via: host CLI -> localhost:7233
  frontend healthy

== 2. Nexus API
  nexus endpoint API answers (enabled by default on 1.27.4+)

== 3. Frontend HTTP port (callbacks arrive here)
  httpPort 7243 present in the rendered server config
  port 7243 is published and answering on the host (HTTP 404 at /)

== 4. Callback dynamic config
  callback endpoint template set
  callback allow-list set
  AllowInsecure: true and Pattern '*' — development only. See SECURITY.md D11.

== 5. Registered endpoints
  none registered yet — that is a clean slate, not a fault

Nexus is configured.
```

A 404 at `/` on port 7243 is the **correct** healthy response — there is no
handler at the root, only under `/namespaces/...`. (Checking this with `curl -f`
reports a working port as broken, because `-f` treats 4xx as failure.)

---

## Configuration, and why each piece is there

### 1. Publish the HTTP port — `docker-compose.yml`

```yaml
ports:
  - "7243:7243"   # HTTP — Nexus callbacks and the frontend HTTP API
```

Only needed for a handler **outside** the cluster calling back in. For
cross-Namespace Nexus within one cluster the server resolves the target through
membership and never dials the callback URL over the network.

### 2. Callback configuration — `temporal/dynamicconfig/development.yaml`

Mounted over `/etc/temporal/config/dynamicconfig/docker.yaml`, which auto-setup
ships as an **empty** file and polls every 60 seconds. So edits apply without a
restart — and "I changed it and nothing happened" is usually the poll interval.

```yaml
component.nexusoperations.callback.endpoint.template:
  - value: http://temporal:7243/namespaces/{{.NamespaceName}}/nexus/callback

component.callbacks.allowedAddresses:
  - value:
      - Pattern: "*"
        AllowInsecure: true
```

`{{.NamespaceName}}` is a **Go template** interpolated by the server — not a
shell or Compose variable. Leave the braces alone.

> **These are the pre-1.30 keys.** On Server **1.30.X and later** both are
> replaced by a single `component.nexusoperations.useSystemCallbackURL: true`
> and the server derives the URL itself. Check your version before copying this
> file into anything.

> **`Pattern: "*"` with `AllowInsecure: true` is development-only.** It accepts
> a callback to any host, in cleartext — a data-exfiltration path and an SSRF
> primitive. Narrow the pattern to your own domain and set `AllowInsecure:
> false` once TLS terminates in front of the frontend. Tracked as **D11** in
> [SECURITY.md](../../SECURITY.md).

---

## Managing Namespaces

`scripts/tctl.sh` wraps the CLI. It exists for one reason: **auto-setup binds
the Frontend to the container's own eth0 address**, not to `0.0.0.0` and not to
loopback, so inside the container `--address 127.0.0.1:7233` is refused and
every command needs `--address $(hostname -i):7233`. From the host,
`localhost:7233` works because Compose publishes it. The script detects which
side it is on and resolves the address either way.

```bash
make ns-list
make ns-create NAME=payments RETENTION=168h
make ns-delete NAME=payments          # prompts; deletes every history in it

# or directly, with more flags
./scripts/tctl.sh ns describe payments
./scripts/tctl.sh ns retention payments 720h
./scripts/tctl.sh ns update payments --description "Payments team"
```

**Set retention explicitly at creation.** The server default is 72h. A team that
assumes 30 days finds out when they go looking for a two-week-old execution and
it is gone — and raising retention later does not bring back what has already
been deleted.

---

## Managing Endpoints

```bash
make nexus-list
make nexus-create EP=payments-api NS=payments TQ=billing
make nexus-delete EP=payments-api

# or directly
./scripts/tctl.sh nexus get payments-api
./scripts/tctl.sh nexus create payments-api \
    --namespace payments --task-queue billing --description "Payments Nexus service"
./scripts/tctl.sh nexus update payments-api --task-queue billing-v2
```

An Endpoint is a **name** that routes to a `(namespace, task-queue)` pair.

Two asymmetries worth internalising:

- **The target Namespace must exist.** `tctl.sh` checks and refuses with the
  command to create it, because the raw CLI's error here is not obvious.
- **The target Task Queue need not exist**, and never errors. A Task Queue is
  created implicitly by its first poller, so a typo produces an Endpoint that
  resolves cleanly and whose Operations then sit pending forever. Nothing will
  tell you. Start the handler Worker and confirm the Operation completes.

`update` leaves unspecified fields unchanged, so partial updates are safe. But
re-pointing an Endpoint takes effect for **every caller at once**, with no
version skew and no deploy — the feature and the hazard in one action.

---

## Verifying it end to end

`nexus-doctor` proves the *configuration*. It cannot prove an Operation
completes, because that needs a handler Worker, which this demo app does not
include. To close the loop, follow the SDK guide for your language, then:

```bash
make nexus-create EP=<endpoint> NS=<namespace> TQ=<queue>   # register
# start your handler Worker polling <namespace>/<queue>
# invoke the Operation from a caller Workflow
```

If registration succeeds and the Operation never completes, check in this order:

1. `make nexus-doctor` — is the callback config present at all?
2. Is a Worker actually polling that exact Task Queue in that exact Namespace?
3. The 60s dynamic-config poll — did you change config less than a minute ago?
4. `docker logs tobs-temporal | grep -i nexus`

---

## Scope

**The Endpoint registry is per-cluster and is not replicated.** In a multi-cluster
deployment, Endpoints must be registered on each cluster separately. Nothing
warns you about the ones you missed.

For **Temporal Cloud**, Nexus Endpoints are managed through the Cloud API rather
than this CLI path — see `terraform/patterns/02-nexus-cross-team/` in this repo,
which models the same cross-team problem with the Cloud provider.
