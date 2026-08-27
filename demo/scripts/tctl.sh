#!/usr/bin/env bash
#
# Namespace and Nexus Endpoint management for the demo cluster.
#
# WHY THIS EXISTS RATHER THAN "just run the temporal CLI":
#
# auto-setup binds the Frontend to the container's own eth0 address, NOT to
# 0.0.0.0 and not to loopback (BIND_ON_IP defaults to the container IP). So
# inside the container `--address 127.0.0.1:7233` is refused, and every command
# needs `--address $(hostname -i):7233`. That one detail is responsible for most
# "the CLI cannot reach the server" confusion on this stack, and it is exactly
# the kind of thing nobody should have to rediscover.
#
# From the HOST, localhost:7233 works fine because Compose publishes the port —
# so this script prefers a local `temporal` binary and falls back to the one
# inside the container, resolving the address correctly either way.
#
# Usage:
#   ./scripts/tctl.sh ns list
#   ./scripts/tctl.sh ns create payments --retention 72h --description "Payments team"
#   ./scripts/tctl.sh ns describe payments
#   ./scripts/tctl.sh ns retention payments 168h
#   ./scripts/tctl.sh ns delete payments
#
#   ./scripts/tctl.sh nexus list
#   ./scripts/tctl.sh nexus create payments-api --namespace payments --task-queue billing
#   ./scripts/tctl.sh nexus get payments-api
#   ./scripts/tctl.sh nexus update payments-api --task-queue billing-v2
#   ./scripts/tctl.sh nexus delete payments-api
#
#   ./scripts/tctl.sh doctor        # is Nexus actually configured, end to end?

set -uo pipefail

CONTAINER="${TEMPORAL_CONTAINER:-tobs-temporal}"

red()  { printf "\033[31m%s\033[0m\n" "$1"; }
grn()  { printf "\033[32m%s\033[0m\n" "$1"; }
ylw()  { printf "\033[33m%s\033[0m\n" "$1"; }
hdr()  { printf "\n\033[1m== %s\033[0m\n" "$1"; }

# Prefer a host CLI; fall back to the container's. The address differs between
# the two, which is the whole reason this indirection exists.
if command -v temporal >/dev/null 2>&1 && \
   temporal operator cluster health --address localhost:7233 >/dev/null 2>&1; then
  t() { temporal "$@" --address localhost:7233; }
  WHERE="host CLI -> localhost:7233"
elif docker inspect "$CONTAINER" >/dev/null 2>&1; then
  t() {
    local args; args=$(printf ' %q' "$@")
    docker exec "$CONTAINER" sh -c "temporal${args} --address \$(hostname -i):7233"
  }
  WHERE="container CLI -> $CONTAINER"
else
  red "No route to Temporal: no working host CLI, and container '$CONTAINER' is not running."
  echo "Start the stack with 'make up', or set TEMPORAL_CONTAINER."
  exit 1
fi

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

cmd="${1:-}"; shift || true

case "$cmd" in

# ---------------------------------------------------------------------------
ns|namespace)
  sub="${1:-list}"; shift || true
  case "$sub" in
    list)     t operator namespace list ;;
    describe) [ $# -ge 1 ] || { red "usage: ns describe <name>"; exit 2; }
              t operator namespace describe --namespace "$1" ;;
    create)
      [ $# -ge 1 ] || { red "usage: ns create <name> [--retention 72h] [--description ...]"; exit 2; }
      name="$1"; shift
      # Retention is REQUIRED in spirit even though the flag is optional: the
      # server default is 72h, and a team that assumes 30 days gets a nasty
      # surprise the first time they go looking for a two-week-old execution.
      # Setting it explicitly at creation is cheap; changing it later does not
      # retroactively recover what has already been deleted.
      t operator namespace create --namespace "$name" "$@" \
        && grn "namespace '$name' created" \
        && ylw "  retention: $(t operator namespace describe --namespace "$name" 2>/dev/null | grep -i retention || echo unknown)"
      ;;
    retention)
      [ $# -ge 2 ] || { red "usage: ns retention <name> <duration, e.g. 168h>"; exit 2; }
      t operator namespace update --namespace "$1" --retention "$2" \
        && grn "retention for '$1' set to $2"
      ;;
    update)
      [ $# -ge 1 ] || { red "usage: ns update <name> [flags...]"; exit 2; }
      name="$1"; shift
      t operator namespace update --namespace "$name" "$@"
      ;;
    delete)
      [ $# -ge 1 ] || { red "usage: ns delete <name>"; exit 2; }
      # Deleting a Namespace deletes its Workflow histories. There is no undo
      # and no export step built into the command.
      ylw "This deletes Namespace '$1' AND every Workflow history in it. No undo."
      printf "Type the namespace name to confirm: "; read -r confirm
      [ "$confirm" = "$1" ] || { red "aborted"; exit 1; }
      t operator namespace delete --namespace "$1" --yes
      ;;
    *) red "unknown: ns $sub"; usage 2 ;;
  esac
  ;;

# ---------------------------------------------------------------------------
nexus)
  sub="${1:-list}"; shift || true
  case "$sub" in
    list) t operator nexus endpoint list ;;
    get)  [ $# -ge 1 ] || { red "usage: nexus get <endpoint>"; exit 2; }
          t operator nexus endpoint get --name "$1" ;;
    create)
      # An Endpoint is a NAME that routes to a (namespace, task queue) pair.
      # Callers reference the name only — which is the point: the handler can
      # move Namespace or Task Queue without every caller being redeployed.
      ep=""; ns=""; tq=""; desc=""
      ep="${1:-}"; shift || true
      [ -n "$ep" ] || { red "usage: nexus create <endpoint> --namespace <ns> --task-queue <tq> [--description ...]"; exit 2; }
      while [ $# -gt 0 ]; do
        case "$1" in
          --namespace)   ns="$2"; shift 2 ;;
          --task-queue)  tq="$2"; shift 2 ;;
          --description) desc="$2"; shift 2 ;;
          *) red "unknown flag: $1"; exit 2 ;;
        esac
      done
      [ -n "$ns" ] && [ -n "$tq" ] || { red "--namespace and --task-queue are both required"; exit 2; }
      # The target Namespace must already exist; the Task Queue need not — a
      # queue is created implicitly by the first poller, so a typo here produces
      # an Endpoint that resolves and then silently never completes an
      # Operation. Check the Namespace so at least half of that is caught.
      if ! t operator namespace describe --namespace "$ns" >/dev/null 2>&1; then
        red "target namespace '$ns' does not exist — create it first:"
        echo "    ./scripts/tctl.sh ns create $ns --retention 72h"
        exit 1
      fi
      set -- operator nexus endpoint create --name "$ep" --target-namespace "$ns" --target-task-queue "$tq"
      [ -n "$desc" ] && set -- "$@" --description "$desc"
      t "$@" && grn "endpoint '$ep' -> ${ns}/${tq}" \
             && ylw "  a Task Queue is created by its first poller, so '$tq' having no"  \
             && ylw "  Worker yet is NOT an error here — and not an error later either." \
             && ylw "  It just means Operations sit pending. Start a handler Worker."
      ;;
    update)
      ep="${1:-}"; shift || true
      [ -n "$ep" ] || { red "usage: nexus update <endpoint> [--namespace ns] [--task-queue tq] [--description ...]"; exit 2; }
      ns=""; tq=""; desc=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --namespace)   ns="$2"; shift 2 ;;
          --task-queue)  tq="$2"; shift 2 ;;
          --description) desc="$2"; shift 2 ;;
          *) red "unknown flag: $1"; exit 2 ;;
        esac
      done
      [ -n "$ns$tq$desc" ] || { red "nothing to update"; exit 2; }
      # `update` leaves unspecified fields unchanged, so partial updates are
      # safe — but re-pointing an Endpoint is live for every caller at once,
      # with no version skew and no deploy. That is the feature and the hazard.
      set -- operator nexus endpoint update --name "$ep"
      [ -n "$ns" ]   && set -- "$@" --target-namespace "$ns"
      [ -n "$tq" ]   && set -- "$@" --target-task-queue "$tq"
      [ -n "$desc" ] && set -- "$@" --description "$desc"
      t "$@" && grn "endpoint '$ep' updated"
      ;;
    delete)
      [ $# -ge 1 ] || { red "usage: nexus delete <endpoint>"; exit 2; }
      t operator nexus endpoint delete --name "$1" ;;
    *) red "unknown: nexus $sub"; usage 2 ;;
  esac
  ;;

# ---------------------------------------------------------------------------
# Nexus has a specific failure mode: registration succeeds with no callback
# configuration at all, and the first real Operation invocation is what fails.
# This checks the parts that registration does not.
doctor)
  fail=0

  hdr "1. Reaching the server"
  echo "    via: $WHERE"
  if t operator cluster health >/dev/null 2>&1; then grn "  frontend healthy"
  else red "  frontend unreachable"; fail=1; fi

  hdr "2. Nexus API"
  if t operator nexus endpoint list >/dev/null 2>&1; then
    grn "  nexus endpoint API answers (enabled by default on 1.27.4+)"
  else
    red "  nexus endpoint API rejected the call"; fail=1
  fi

  hdr "3. Frontend HTTP port (callbacks arrive here)"
  if docker exec "$CONTAINER" sh -c 'grep -q "httpPort: 7243" /etc/temporal/config/docker.yaml' 2>/dev/null; then
    grn "  httpPort 7243 present in the rendered server config"
  else
    red "  no httpPort in the rendered config — callbacks have nowhere to land"; fail=1
  fi
  # No -f. The frontend answers `/` with 404 {"code":5,"message":"Not Found"},
  # which is a correct, healthy response — there is no handler at the root, only
  # under /namespaces/... . `curl -f` treats 4xx as failure and prints nothing,
  # so it reported this working port as unreachable.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:7243/ 2>/dev/null)
  if printf '%s' "$code" | grep -qE '^(200|400|404)$'; then
    grn "  port 7243 is published and answering on the host (HTTP $code at /)"
  else
    ylw "  port 7243 not reachable from the host — fine for cross-namespace Nexus"
    ylw "  inside the cluster, NOT fine for an external handler calling back in."
  fi

  hdr "4. Callback dynamic config"
  # THE CHECK THAT MATTERS. Without these keys, endpoints register and
  # operations fail at invocation time.
  dc=$(docker exec "$CONTAINER" sh -c 'cat /etc/temporal/config/dynamicconfig/docker.yaml' 2>/dev/null)
  if printf '%s' "$dc" | grep -q 'component.nexusoperations.callback.endpoint.template'; then
    grn "  callback endpoint template set"
  else
    red "  callback endpoint template MISSING — endpoints will register and then
       fail at the first Operation invocation. Mount
       demo/temporal/dynamicconfig/development.yaml and wait for the 60s poll."
    fail=1
  fi
  if printf '%s' "$dc" | grep -q 'component.callbacks.allowedAddresses'; then
    grn "  callback allow-list set"
    printf '%s' "$dc" | grep -q 'AllowInsecure: true' && \
      ylw "  AllowInsecure: true and Pattern '*' — development only. See SECURITY.md D11."
  else
    red "  callback allow-list MISSING — the server will reject its own callbacks"; fail=1
  fi

  hdr "5. Registered endpoints"
  out=$(t operator nexus endpoint list 2>/dev/null)
  if [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
    ylw "  none registered yet — that is a clean slate, not a fault"
  else
    printf '%s\n' "$out"
  fi

  echo
  [ "$fail" -eq 0 ] && grn "Nexus is configured." || { red "Nexus is NOT fully configured."; exit 1; }
  ;;

# Escape hatch: anything this wrapper does not model, with the address solved.
raw) t "$@" ;;

""|-h|--help|help) usage 0 ;;
*) red "unknown command: $cmd"; usage 2 ;;
esac
