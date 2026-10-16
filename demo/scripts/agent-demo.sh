#!/usr/bin/env bash
#
# Populate every panel on the AI agent board.
#
# The board is driven entirely by gen_ai_* series, which do not exist until an
# agent has actually run. Opening it on a fresh stack shows a full page of "No
# data" — which is correct, and is also the single worst first impression a
# dashboard can make, because it is indistinguishable from broken.
#
# This drives enough varied traffic that every panel has something in it,
# including the ones that only populate on a FAILURE. Those matter most: a
# panel that has never rendered a value is a panel nobody trusts during an
# incident, and several here are supposed to be empty when things are healthy.
#
# Usage:
#   scripts/agent-demo.sh [runs] [provider]
#   scripts/agent-demo.sh 30 mock
#
set -euo pipefail

cd "$(dirname "$0")/.."

RUNS="${1:-20}"
PROVIDER="${2:-mock}"
API="${API_BASE:-http://localhost:8081}"

if [ "$PROVIDER" != "mock" ]; then
  echo "WARNING: provider '$PROVIDER' makes REAL API calls and costs REAL money."
  echo "         $RUNS runs at several model calls each. Ctrl-C now if unintended."
  sleep 5
fi

# HTTP error statuses are fine here — the error-path runs exist to produce
# them. A curl FAILURE (API down, connection refused) is not: it used to be
# swallowed by `|| true`, so a dead API still ended with "done".
unreachable=0
post() {
  curl -s --max-time 120 -X POST "$API/agent" \
    -H 'content-type: application/json' -d "$1" >/dev/null || unreachable=$((unreachable + 1))
}

echo "==> $RUNS agent runs via provider '$PROVIDER'"

# 1. Healthy runs. Varied prompts so the mock's hash-seeded latency and token
#    counts differ per run and the histograms get a real distribution rather
#    than a single spike.
echo "    [1/4] healthy runs"
for i in $(seq 1 "$RUNS"); do
  post "{\"prompt\":\"Where is order order-$i and what will it cost to ship to city-$i?\",\"provider\":\"$PROVIDER\"}"
done

# 2. Tool failures. Exercises the tool-call outcome panel AND shows the model
#    recovering — the recovery is billable, which is the point worth seeing.
echo "    [2/4] runs with injected tool failures"
for i in $(seq 1 5); do
  post "{\"prompt\":\"Check stock for SKU-$i and quote shipping\",\"provider\":\"$PROVIDER\",\"failToolRate\":0.4}"
done

# 3. Runaway agents. Populates the max_iterations outcome and pushes the loop
#    depth p99 to the cap. THE MOST IMPORTANT PANEL ON THE BOARD, because no
#    Temporal metric moves for this at all.
echo "    [3/4] runaway agents (loop to the cap)"
for i in $(seq 1 3); do
  post "{\"prompt\":\"runaway $i\",\"provider\":\"$PROVIDER\",\"loopForever\":true,\"maxIterations\":6}"
done

# 4. A deliberately bad provider name.
#
#    This exists because the error panel was EMPTY the first time it was
#    checked — the unknown-provider branch returned before recording a metric,
#    so a misconfigured provider was the one failure the board could not see.
#    Keeping it in the demo means the regression cannot come back unnoticed.
echo "    [4/4] provider misconfiguration (error path)"
post "{\"prompt\":\"error path\",\"provider\":\"not-a-provider\"}"

if [ "$unreachable" -gt 0 ]; then
  echo "ERROR: $unreachable request(s) never reached $API — is the api service up? (make up)" >&2
  exit 1
fi

echo
echo "==> done. Allow ~15s for the collector's export interval, then open:"
echo "    http://localhost:3000/d/ai-agent-observability"
echo
echo "    Verify every panel has data with:"
echo "      make dashboards-check"
