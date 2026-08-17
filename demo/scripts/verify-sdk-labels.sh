#!/usr/bin/env bash
#
# Verify that this repo's SDK-metric alerts actually match YOUR SDK's output.
#
# WHY THIS EXISTS
#
# The non-determinism alert asserts a specific label NAME. Temporal's own
# published guidance gives it as:
#
#     temporal_workflow_task_execution_failed_total{error_type="NonDeterminismError"}
#
# On the Go SDK via tally the label is actually `failure_reason`. Verified by
# forcing a real NDE on this stack and reading the exported series. The value is
# right; the label name is not — and copied verbatim that rule matches nothing
# and yields a permanently silent alert for the one failure Temporal cannot retry
# past, while looking perfectly correct in review.
#
# That was found on Go. Java, TypeScript, Python and .NET may differ again, and a
# comment nobody reads before an incident is not protection. Run this at setup.
#
# Usage:  make verify-sdk-labels           (or) ./scripts/verify-sdk-labels.sh
set -uo pipefail

PROM="${PROM:-http://localhost:9090}"
FAIL=0

say()  { printf "  %s\n" "$1"; }
ok()   { printf "  \033[32m PASS\033[0m  %s\n" "$1"; }
bad()  { printf "  \033[31m FAIL\033[0m  %s\n" "$1"; FAIL=1; }
warn() { printf "  \033[33m WARN\033[0m  %s\n" "$1"; }

printf "\n\033[1m== SDK metric label verification\033[0m\n"

# The families whose LABELS this repo's alerts depend on.
families=$(curl -sf "$PROM/api/v1/label/__name__/values" 2>/dev/null | python3 -c "
import json,sys
try: names=json.load(sys.stdin)['data']
except Exception: sys.exit(1)
want=[n for n in names if 'workflow_task_execution_failed' in n or 'activity_execution_failed' in n]
print('\n'.join(sorted(want)))" 2>/dev/null)

if ! printf '%s\n' "$families" | grep -q "workflow_task_execution_failed"; then
  warn "temporal_workflow_task_execution_failed* does not exist yet."
  say  "  It is a counter — it appears only after a Workflow Task has failed, so"
  say  "  the NDE alert's label CANNOT be verified until one has."
  say  "  Produce one and re-run:   make chaos-nde     (then make chaos-nde-stop)"
  say  ""
  say  "  Until then, treat TemporalNonDeterminismError as UNVERIFIED on this SDK."
  printf "\n"
  exit 0
fi

if [ -z "$families" ]; then
  warn "no workflow/activity failure metric families exist yet."
  say  "  These are counters — they do not appear until a failure has happened."
  say  "  Produce one and re-run:   make chaos-nde     (then make chaos-nde-stop)"
  say  ""
  say  "  Until then the NDE alert is UNVERIFIED on this SDK."
  exit 0
fi

say "families present:"
for f in $families; do say "  - $f"; done
say ""

# What does the shipped alert select on?
expected_label="failure_reason"
expected_value="NonDeterminismError"

# ONLY the workflow-task family carries the non-determinism reason. Checking the
# activity family for it produces a confusing false alarm telling you to edit the
# NDE alert based on a metric that has nothing to do with it — which this script
# did on its first run.
for fam in $families; do
  case "$fam" in
    *workflow_task_execution_failed*) ;;
    *) say "$fam  (listed for reference; the NDE label lives on the workflow-task family)"
       say ""
       continue ;;
  esac
  labels=$(curl -sf "$PROM/api/v1/series?match[]=$fam" 2>/dev/null | python3 -c "
import json,sys
try: r=json.load(sys.stdin)['data']
except Exception: sys.exit(1)
print(','.join(sorted({k for s in r for k in s if k not in ('__name__','instance','job')})))" 2>/dev/null)
  [ -z "$labels" ] && continue
  say "$fam"
  say "  labels: $labels"

  case ",$labels," in
    *",$expected_label,"*)
      ok "$fam carries '$expected_label' — the shipped alert selector matches this SDK" ;;
    *",error_type,"*)
      bad "$fam uses 'error_type', NOT '$expected_label'."
      say  "       The shipped TemporalNonDeterminismError rule will NEVER FIRE here."
      say  "       Edit prometheus/alerts.yml to select on error_type instead." ;;
    *)
      warn "$fam has neither '$expected_label' nor 'error_type'."
      say  "       Pick the label above that carries the failure reason and update"
      say  "       TemporalNonDeterminismError to match it." ;;
  esac
  say ""
done

# If an NDE has actually occurred, confirm the VALUE too — a right label with a
# different value string is the same silent failure.
nde=$(curl -sf -G "$PROM/api/v1/query" --data-urlencode \
  "query=count by ($expected_label) (temporal_workflow_task_execution_failed_total)" 2>/dev/null | python3 -c "
import json,sys
try: r=json.load(sys.stdin)['data']['result']
except Exception: sys.exit(1)
print(','.join(sorted(m['metric'].get('$expected_label','?') for m in r)))" 2>/dev/null)

if [ -n "$nde" ]; then
  say "observed failure reasons: $nde"
  case ",$nde," in
    *"$expected_value"*) ok "value '$expected_value' matches the shipped alert" ;;
    *) warn "no '$expected_value' observed yet — run 'make chaos-nde' to confirm the VALUE string too" ;;
  esac
fi

# Units are the other silent 1000x error, and they are language-dependent.
printf "\n\033[1m== Latency units\033[0m\n"
if curl -sf "$PROM/api/v1/label/__name__/values" 2>/dev/null | grep -q "schedule_to_start_latency_seconds"; then
  ok "histograms are in SECONDS (Go/Java) — shipped thresholds like 0.2 are correct"
else
  if curl -sf "$PROM/api/v1/label/__name__/values" 2>/dev/null | grep -q "schedule_to_start_latency"; then
    bad "histograms have no _seconds suffix — this SDK emits MILLISECONDS."
    say  "       Every latency threshold in alerts.yml is 1000x too small."
    say  "       200ms is 0.2 in Go/Java but 200 in TypeScript/Python/.NET."
  else
    warn "no schedule-to-start histograms found yet — run some traffic first"
  fi
fi

printf "\n"
[ "$FAIL" -eq 0 ] && printf "  \033[32mSDK labels match the shipped alerts.\033[0m\n\n" \
                  || printf "  \033[31mEdit alerts.yml before trusting these alerts.\033[0m\n\n"
exit $FAIL
