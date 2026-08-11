#!/usr/bin/env bash
#
# SCENARIO 5 — Worker blackout and recovery
#
# The only scenario that needs infrastructure chaos rather than load shaping,
# so it lives in bash instead of k6.
#
# Drives steady baseline traffic, kills the entire Worker fleet mid-run, then
# brings it back scaled up. This is the closest thing to a real production
# incident in the set: a deploy gone wrong, a crash loop, a node drain.
#
# WHAT YOU ARE DEMONSTRATING
#   1. Temporal does not lose the work. Nothing fails during the blackout —
#      Tasks queue and wait. That durability guarantee is the entire product
#      pitch, and watching the queue drain on recovery makes it concrete in a
#      way a slide cannot.
#   2. The blind spot: while Workers are down there are NO SDK metrics at all,
#      because SDK metrics come from the Workers. The Worker Fleet Health row
#      goes blank rather than red. Cluster-side signals (no_poller_tasks, sync
#      match rate) are what tell you anything is wrong.
#
# That second point is the one customers consistently miss. An SDK-metrics-only
# monitoring setup is blind to exactly the failure it most needs to catch:
# an absent Worker emits no metrics, and "no data" is not an alert unless you
# deliberately made it one.
#
# Usage: ./scripts/chaos-worker-blackout.sh [blackout_seconds] [recovery_scale]

set -euo pipefail

BLACKOUT="${1:-120}"
SCALE="${2:-3}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT"

echo "========================================================================"
echo "  SCENARIO 5: Worker blackout"
echo "========================================================================"
echo "  Blackout duration : ${BLACKOUT}s"
echo "  Recovery scale    : ${SCALE} workers"
echo ""
echo "  Watch in Grafana:"
echo "    - Tasks With No Poller  -> spikes during blackout"
echo "    - Sync Match Rate       -> collapses during blackout"
echo "    - Worker Task Slots     -> series DISAPPEARS (no data, not zero)"
echo "    - Workflow Outcomes     -> pauses, then floods green on recovery"
echo "========================================================================"
echo ""

echo "==> Starting background load (6m baseline)"
# --rm and -d cannot be combined on `docker compose run`, so the detached
# container is cleaned up explicitly at the end instead.
LOAD_CID=$(docker compose --profile tools run -d \
  -e DURATION=6m k6 run /scripts/00-baseline.js)
trap 'docker rm -f "$LOAD_CID" >/dev/null 2>&1 || true' EXIT

echo "==> Letting traffic settle for 60s"
sleep 60

echo "==> BLACKOUT: stopping all workers"
docker compose stop worker

echo "==> Workers down. Holding for ${BLACKOUT}s."
echo "    Note the Worker Fleet Health panels going to NO DATA, not to zero."
for i in $(seq "$BLACKOUT" -10 10); do
  printf '\r    %3ds remaining ' "$i"
  sleep 10
done
printf '\r%40s\r' ' '

echo "==> RECOVERY: restoring workers at scale=${SCALE}"
docker compose up -d --scale worker="$SCALE" worker

echo ""
echo "==> Workers restored. Watch the backlog drain."
echo "    Prometheus picks up the new replicas through Compose DNS within ~10s."
echo "    Nothing was lost — every queued Task will now execute."
echo ""
echo "    Reset to a single worker when finished:"
echo "      docker compose up -d --scale worker=1 worker"

# Load container is removed by the EXIT trap.
