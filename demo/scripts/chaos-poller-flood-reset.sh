#!/usr/bin/env bash
#
# SCENARIO 8 CLEANUP — return the Worker fleet to one replica.
#
# THIS IS NOT OPTIONAL, and it is the cleanup most likely to be skipped because
# nothing looks broken when you forget it.
#
# Scenario 1 (backlog storm) and scenario 4 (slot saturation) both depend on
# ONE Worker being overwhelmed. Left at 8 replicas they have 8x the slots, the
# backlog never builds, schedule-to-start never climbs, and both scenarios look
# like they failed to reproduce — with no error anywhere to explain why.
#
# Usage: ./scripts/chaos-poller-flood-reset.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROM="${PROM:-http://localhost:9090}"
cd "$ROOT"

before=$(docker compose ps -q worker | wc -l | tr -d ' ')

# `docker compose up -d --scale worker=1 worker` IS NOT ENOUGH, and this was
# found the hard way: it stops the surplus replicas but LEAVES THE CONTAINERS
# IN PLACE. A later compose command in the same project brings them back, and
# the fleet silently returns to a mixed state — measured here as 11 running and
# 9 exited after a "successful" scale-down to 1.
#
# The consequence is exactly the failure this script exists to prevent:
# scenario 4 was then run against 11 Workers instead of 1, schedule-to-start
# never rose above 0.05s, slots never hit zero, and the scenario looked like it
# simply did not reproduce. No error anywhere.
#
# So remove every Worker container first, then create exactly one. Destructive
# and deterministic beats tidy and conditional.
echo "==> Removing all Worker containers (was ${before} running)"
docker compose rm -sf worker >/dev/null 2>&1 || true

echo "==> Creating exactly 1 Worker"
docker compose up -d --scale worker=1 worker >/dev/null

echo "==> Waiting for the fleet to settle"
sleep 10

# Count ALL worker containers, not just running ones (`ps -q` hides stopped
# containers, which is what made the original scale-down look like it worked).
after=$(docker compose ps -q worker | wc -l | tr -d ' ')
total=$(docker compose ps -aq worker | wc -l | tr -d ' ')
echo "    worker containers: ${after} running, ${total} total"

if [ "$after" -ne 1 ] || [ "$total" -ne 1 ]; then
  echo ""
  echo "  WARNING: expected exactly 1 Worker container, found ${after} running"
  echo "  and ${total} total. Any surplus — even STOPPED — can be restarted by"
  echo "  the next compose command, and scenarios 1 and 4 will not reproduce."
  echo ""
  echo "  Force it:  docker compose rm -sf worker && docker compose up -d worker"
  exit 1
fi

# Poll success rate does not recover the instant the Workers stop. The 2m rate
# windows still contain the flood's empty polls, and the metric climbs back
# over roughly the window length. Reporting "recovered" before that is how a
# reset gets wrongly blamed for not working.
echo ""
echo "  Fleet is back to 1 Worker."
echo ""
echo "  Poll success rate recovers over about 2 minutes — the rate() windows"
echo "  still hold the flood's empty polls. Give it that before concluding"
echo "  anything from the panel."
echo ""
echo "  Verify recovery:"
echo "    curl -sG ${PROM}/api/v1/query --data-urlencode \\"
echo "      'query=sum(rate(poll_success{taskqueue=\"orders\",namespace=\"default\"}[2m]))"
echo "             / clamp_min(sum(rate(poll_success{taskqueue=\"orders\",namespace=\"default\"}[2m]))"
echo "             + sum(rate(poll_timeouts{taskqueue=\"orders\",namespace=\"default\"}[2m])), 0.001)'"
echo ""
echo "  Scenarios 1 and 4 will now reproduce correctly."
