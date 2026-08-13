#!/usr/bin/env bash
#
# Minimum observability conformance check for a Temporal application team.
#
# Answers one question: is this team's Namespace + Task Queue observable to the
# minimum standard? Run it yourself before you ship, or have the platform team
# run it against a tenant.
#
#   NAMESPACE=my-team-prod TASK_QUEUE=orders ./conformance-check.sh
#   PROM=http://prometheus:9090 NAMESPACE=... TASK_QUEUE=... ./conformance-check.sh
#
# Exits non-zero if any REQUIRED check fails, so it works as a CI gate.

set -uo pipefail

PROM="${PROM:-http://localhost:9090}"
NAMESPACE="${NAMESPACE:-}"
TASK_QUEUE="${TASK_QUEUE:-}"

if [ -z "$NAMESPACE" ] || [ -z "$TASK_QUEUE" ]; then
  echo "usage: NAMESPACE=<ns> TASK_QUEUE=<tq> [PROM=<url>] $0"
  exit 2
fi

PASS=0; FAIL=0; WARN=0
ok()   { printf "  \033[32m PASS\033[0m  %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m FAIL\033[0m  %s\n"  "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m WARN\033[0m  %s\n"  "$1"; WARN=$((WARN+1)); }
hdr()  { printf "\n\033[1m%s\033[0m\n" "$1"; }

SCOPE="namespace=\"$NAMESPACE\", task_queue=\"$TASK_QUEUE\""

q() {  # returns series count for an instant query, or 0
  curl -sf "$PROM/api/v1/query" --data-urlencode "query=$1" 2>/dev/null | python3 -c "
import json,sys
try: print(len(json.load(sys.stdin)['data']['result']))
except Exception: print(0)" 2>/dev/null || echo 0
}

printf "\n\033[1mTemporal minimum observability conformance\033[0m\n"
printf "  namespace  %s\n  task queue %s\n  prometheus %s\n" "$NAMESPACE" "$TASK_QUEUE" "$PROM"

# ---------------------------------------------------------------------------
hdr "1. Are SDK metrics being exported at all?  (REQUIRED — nothing works without this)"
# ---------------------------------------------------------------------------
if [ "$(q "temporal_worker_task_slots_available{$SCOPE}")" -gt 0 ]; then
  ok "Worker SDK metrics are present"
else
  bad "NO SDK metrics for this namespace/task queue."
  echo "         Your Worker is not exporting, is not being scraped, or the"
  echo "         namespace/task_queue labels differ from what you passed."
  echo "         Nothing else in this standard can work until this passes."
fi

# ---------------------------------------------------------------------------
hdr "2. Can you see whether work is being delivered?  (REQUIRED)"
# ---------------------------------------------------------------------------
if [ "$(q "temporal_activity_schedule_to_start_latency_seconds_count{$SCOPE}")" -gt 0 ]; then
  ok "Activity schedule-to-start histogram present"
else
  warn "No schedule-to-start data. This histogram does not exist until an"
  echo "         Activity has actually run — expected on a queue that has never"
  echo "         processed work. Run a Workflow and re-check."
fi

# ---------------------------------------------------------------------------
hdr "3. Can you see Workflow outcomes?  (REQUIRED)"
# ---------------------------------------------------------------------------
if [ "$(q "temporal_workflow_completed_total{$SCOPE}")" -gt 0 ]; then
  ok "Workflow completion counter present"
else
  warn "No completed Workflows yet on this queue."
fi
if [ "$(q "temporal_workflow_failed_total{$SCOPE}")" -gt 0 ]; then
  ok "Workflow failure counter present (something has failed at some point)"
else
  warn "temporal_workflow_failed_total absent — NORMAL if nothing has ever"
  echo "         failed. It is a counter; it springs into existence on first use."
  echo "         Do NOT write alerts that assume it exists (the shipped rules"
  echo "         use an 'or ... * 0' guard for exactly this)."
fi

# ---------------------------------------------------------------------------
hdr "4. Is capacity visible?  (REQUIRED)"
# ---------------------------------------------------------------------------
if [ "$(q "temporal_worker_task_slots_used{$SCOPE}")" -gt 0 ]; then
  ok "Task slot utilisation is computable"
else
  bad "temporal_worker_task_slots_used missing — cannot tell saturation from idle"
fi

# ---------------------------------------------------------------------------
hdr "5. Is end-to-end latency visible?  (RECOMMENDED)"
# ---------------------------------------------------------------------------
if [ "$(q "temporal_workflow_endtoend_latency_seconds_count{$SCOPE}")" -gt 0 ]; then
  ok "Workflow end-to-end latency histogram present"
else
  warn "No end-to-end latency data. Without it the workflow_latency SLO cannot"
  echo "         be computed — this is the metric closest to what your users feel."
fi

# ---------------------------------------------------------------------------
hdr "5b. Do your SLO latency boundaries actually EXIST as buckets?  (REQUIRED)"
# ---------------------------------------------------------------------------
# Two ways this silently breaks, both seen on a real SDK:
#   * `le` is a STRING match — le="1" does not match le="1.0"
#   * the default SDK buckets top out at 10s, so a 60s boundary matches nothing
# Either way the SLI produces NO SERIES AT ALL — not a wrong number, no number.
S2S_LE="${S2S_LE:-1.0}"
E2E_LE="${E2E_LE:-10.0}"
S2S_Q="temporal_activity_schedule_to_start_latency_seconds_bucket{$SCOPE, le=\"$S2S_LE\"}"
E2E_Q="temporal_workflow_endtoend_latency_seconds_bucket{$SCOPE, le=\"$E2E_LE\"}"
if [ "$(q "$S2S_Q")" -gt 0 ]; then
  ok "task_delivery boundary le=\"$S2S_LE\" exists"
else
  bad "NO BUCKET at le=\"$S2S_LE\" — the task_delivery SLI will produce nothing."
  echo "         Available boundaries:"
  curl -sf "$PROM/api/v1/query" --data-urlencode \
    "query=count by (le) (temporal_activity_schedule_to_start_latency_seconds_bucket{$SCOPE})" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    r=json.load(sys.stdin)['data']['result']
    v=sorted(set(m['metric']['le'] for m in r), key=lambda x: float(x) if x!='+Inf' else 1e18)
    print('           ' + ', '.join(v))
except Exception: print('           (none)')"
fi
if [ "$(q "$E2E_Q")" -gt 0 ]; then
  ok "workflow_latency boundary le=\"$E2E_LE\" exists"
else
  warn "No bucket at le=\"$E2E_LE\" — the workflow_latency SLI will produce nothing."
  echo "         The SDK defaults stop at 10.0s. For a longer boundary you must"
  echo "         configure custom histogram buckets in your Worker first."
fi

# ---------------------------------------------------------------------------
hdr "6. Are the minimum SLO rules loaded?  (REQUIRED)"
# ---------------------------------------------------------------------------
if [ "$(q "appslo:error_budget_remaining:ratio")" -gt 0 ]; then
  ok "appslo: recording rules are loaded and evaluating"
else
  bad "No appslo:* series. slo-rules.yml is not loaded, or the \$NAMESPACE /"
  echo "         \$TASK_QUEUE placeholders were never substituted."
fi

# ---------------------------------------------------------------------------
hdr "7. Is the absence alert enabled?  (REQUIRED — the one nothing else covers)"
# ---------------------------------------------------------------------------
rules=$(curl -sf "$PROM/api/v1/rules" 2>/dev/null || echo '')
if echo "$rules" | grep -q "AppWorkerFleetAbsent"; then
  ok "AppWorkerFleetAbsent is loaded"
else
  bad "AppWorkerFleetAbsent is NOT loaded. It ships commented out."
  echo "         Substitute your namespace/task queue and uncomment it."
  echo "         When your Workers die their metrics STOP EXISTING rather than"
  echo "         going to zero, so every threshold alert you have goes quiet at"
  echo "         exactly the moment you most need one. This is the only rule"
  echo "         that catches a fleet that is entirely gone."
fi

# ---------------------------------------------------------------------------
hdr "8. Is anyone else's data leaking into your rules?  (hygiene)"
# ---------------------------------------------------------------------------
total_ns=$(q "count by (namespace) (temporal_worker_task_slots_available)")
if [ "$total_ns" -gt 1 ]; then
  warn "This Prometheus carries $total_ns namespaces. Every rule and dashboard"
  echo "         panel you deploy MUST be scoped to namespace=\"$NAMESPACE\" or you"
  echo "         will page on another tenant's incident."
else
  ok "Single namespace in this Prometheus"
fi

printf "\n\033[1mResult\033[0m\n"
printf "  passed %s   failed %s   warnings %s\n\n" "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -gt 0 ]; then
  echo "  NOT CONFORMANT — see the failures above."
  exit 1
fi
echo "  Conformant to the minimum standard."
[ "$WARN" -gt 0 ] && echo "  (warnings are usually 'no traffic yet', not defects)"
exit 0
