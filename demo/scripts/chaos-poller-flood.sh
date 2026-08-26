#!/usr/bin/env bash
#
# SCENARIO 8 — Poller flood (over-provisioned Worker fleet)
#
# Needs infrastructure chaos rather than load shaping — the flood is the FLEET,
# not the load — so it lives in bash beside chaos-worker-blackout.sh rather
# than in k6.
#
# Scales the Worker fleet well past what the offered load needs, then drives a
# quiet healthy baseline underneath it. Most long-polls then wait their full
# 60s and return empty, poll_timeouts swamps poll_success, and POLL SUCCESS
# RATE COLLAPSES WHILE NOTHING IS WRONG.
#
# WHAT YOU ARE DEMONSTRATING
#   1. A starved fleet and a flooded fleet BOTH push poll success rate down,
#      and they need opposite responses. Alerting on that metric alone leads
#      you to add Workers during a flood.
#   2. Sync match rate does NOT follow it down. The two ratios look alike and
#      measure unrelated things — one is health, one is sizing.
#   3. TemporalMatchingStarved (poll success AND schedule-to-start) stays
#      silent through the whole run. That is its acceptance test.
#
# Usage: ./scripts/chaos-poller-flood.sh [workers] [duration]
#        WORKERS=24 DURATION=10m ./scripts/chaos-poller-flood.sh
#
# DEFAULTS ARE MEASURED, NOT GUESSED: 20 Workers against 0.2 orders/sec, i.e.
# ~100 pollers per offered order/sec. See docs/CHAOS-RUNBOOK.md scenario 8 for
# why a smaller ratio does not reproduce at all.

set -euo pipefail

WORKERS="${1:-${WORKERS:-20}}"
DURATION="${2:-${DURATION:-10m}}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROM="${PROM:-http://localhost:9090}"

cd "$ROOT"

# Scoped to the APPLICATION Task Queue, and this is load-bearing rather than
# tidiness. Temporal's own system Task Queues (temporal_sys_*, one per
# namespace) long-poll constantly and almost never match anything, so they
# contribute a large, permanent stream of poll_timeouts with no matching
# poll_success. Measured on this stack: poll_timeouts spans 68 series across 6
# namespaces while poll_success spans 26 across 2.
#
# An unscoped poll success rate is therefore dominated by idle system queues
# and barely moves when the application fleet floods — the scenario appears not
# to reproduce. Scope to the queue you care about.
TQ='taskqueue="orders",namespace="default"'

q() {
  curl -sfG "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null | python3 -c "
import json,sys
try:
    r=json.load(sys.stdin)['data']['result']
    v=float(r[0]['value'][1]) if r else float('nan')
    print('n/a' if v!=v else f'{v:.4f}')
except Exception:
    print('n/a')"
}

snapshot() {
  local label="$1"
  printf "\n  ---- %s ----\n" "$label"
  printf "    poll success rate   : %s\n" \
    "$(q "sum(rate(poll_success{$TQ}[2m])) / clamp_min(sum(rate(poll_success{$TQ}[2m])) + sum(rate(poll_timeouts{$TQ}[2m])), 0.001)")"
  printf "    sync match rate     : %s\n" \
    "$(q "sum(rate(poll_success_sync{$TQ}[2m])) / clamp_min(sum(rate(poll_success{$TQ}[2m])), 0.001)")"
  printf "    schedule-to-start p99: %s s\n" \
    "$(q 'histogram_quantile(0.99, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket[2m])))')"
  printf "    slots available     : %s\n" \
    "$(q 'sum(temporal_worker_task_slots_available{namespace="default"})')"
  printf "    empty polls /s      : %s\n" "$(q "sum(rate(poll_timeouts{$TQ}[2m]))")"
  printf "    matched polls /s    : %s\n" "$(q "sum(rate(poll_success{$TQ}[2m]))")"
}

echo "========================================================================"
echo "  SCENARIO 8: Poller flood — over-provisioned Worker fleet"
echo "========================================================================"
echo "  Worker replicas   : ${WORKERS}   (baseline is 1)"
echo "  Load duration     : ${DURATION}  (quiet and healthy, ~0.2 orders/sec)"
echo ""
echo "  Watch in Grafana, row P:"
echo "    - Poll Outcome Mix       -> EMPTY band grows ~23x (0.03 -> 0.66/s)"
echo "    - Poll Success Rate      -> collapses; the ONLY thing that moves"
echo "    - Sync Match Rate        -> stays ~100%, does NOT follow"
echo "    - Discriminator          -> s2s ~0 and slots free = NOT starvation"
echo ""
echo "  Alerts:  TemporalMatchingStarved            must NOT fire"
echo "           TemporalWorkerFleetOverProvisioned should go pending (info)"
echo "========================================================================"

snapshot "BEFORE — ${WORKERS}x scale not yet applied"

echo ""
echo "==> Scaling Worker fleet to ${WORKERS}"
# Not `make scale-up`: that target is hardcoded to 5 replicas and takes no
# count. Parameterising it is done in the Makefile (WORKERS=), but this script
# is also runnable standalone, so it scales directly.
docker compose up -d --scale worker="${WORKERS}" worker >/dev/null

# Compose DNS discovery is not instant. Scraping a half-discovered fleet
# understates poller count and muddies the shape — the empty band creeps up
# instead of jumping, which reads as a weak effect rather than a clear one.
echo "==> Waiting 20s for Compose DNS discovery and poller registration"
sleep 20

running=$(docker compose ps -q worker | wc -l | tr -d ' ')
echo "    worker containers running: ${running}"
if [ "$running" -lt "$WORKERS" ]; then
  echo "    WARNING: expected ${WORKERS}. The shape will be weaker than documented."
fi

echo ""
echo "==> Driving quiet baseline load for ${DURATION}"
docker compose --profile tools run --rm \
  -e DURATION="${DURATION}" \
  -e ORDER_RATE="${ORDER_RATE:-0.2}" \
  k6 run /scripts/08-poller-flood.js || true

# The 2m rate windows in the queries need time to reflect the flood. Sampling
# the instant k6 exits reads a window that is still half pre-flood.
echo ""
echo "==> Letting the 2m rate windows catch up (45s)"
sleep 45

snapshot "AFTER — ${WORKERS} workers under the same load"

echo ""
echo "========================================================================"
echo "  Poll success rate should have collapsed."
echo "  Sync match rate should be unchanged, near 1.0."
echo "  Schedule-to-start and slots should look completely healthy."
echo ""
echo "  THAT COMBINATION IS THE FINDING: the only metric that moved is the"
echo "  one whose remedy is to REMOVE capacity."
echo ""
echo "  CLEANUP IS REQUIRED:"
echo "      make chaos-poller-flood-reset"
echo ""
echo "  ${WORKERS} Workers left running will absorb the backlog in scenarios"
echo "  1 and 4 and make them look like they failed to reproduce."
echo "========================================================================"
