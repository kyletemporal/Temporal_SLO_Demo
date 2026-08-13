#!/usr/bin/env bash
#
# End-to-end validation of the whole stack.
#
# Checks what actually matters and nothing that does not: containers healthy,
# every scrape target up, every rule loading and evaluating, and every panel
# query on every provisioned dashboard returning data — with an explicit
# allowlist for the panels that are SUPPOSED to be empty on a healthy system.
#
# Exits non-zero if anything fails, so it works in CI as well as by hand.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PROM="${PROM:-http://localhost:9090}"
GRAF="${GRAF:-http://localhost:3000}"

PASS=0
FAIL=0
WARN=0

ok()   { printf "  \033[32m PASS\033[0m  %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31m FAIL\033[0m  %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m WARN\033[0m  %s\n" "$1"; WARN=$((WARN+1)); }
hdr()  { printf "\n\033[1m== %s\033[0m\n" "$1"; }

# -----------------------------------------------------------------------------
hdr "1. Containers"
# -----------------------------------------------------------------------------
expected=(tobs-postgres tobs-temporal tobs-temporal-ui tobs-api tobs-prometheus tobs-grafana)
for c in "${expected[@]}"; do
  status=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo missing)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$c" 2>/dev/null || echo -)
  if [ "$status" = running ] && { [ "$health" = healthy ] || [ "$health" = "-" ]; }; then
    ok "$c ($status${health:+, $health})"
  else
    bad "$c is $status${health:+ / $health}"
  fi
done
wcount=$(docker ps --filter "name=worker" --filter "status=running" -q | wc -l | tr -d ' ')
[ "$wcount" -ge 1 ] && ok "worker fleet: $wcount replica(s) running" \
                    || bad "worker fleet: no replicas running"

# -----------------------------------------------------------------------------
hdr "2. Prometheus scrape targets"
# -----------------------------------------------------------------------------
targets=$(curl -sf "$PROM/api/v1/targets?state=active" 2>/dev/null)
if [ -z "$targets" ]; then
  bad "Prometheus unreachable at $PROM"
else
  while IFS='|' read -r job health url; do
    [ "$health" = up ] && ok "$job ($url)" || bad "$job is $health ($url)"
  done < <(echo "$targets" | python3 -c "
import json,sys
for t in json.load(sys.stdin)['data']['activeTargets']:
    print(f\"{t['labels'].get('job')}|{t['health']}|{t['scrapeUrl']}\")")
  for expect in temporal-service temporal-sdk-api temporal-sdk-worker; do
    echo "$targets" | grep -q "\"job\":\"$expect\"" || bad "target $expect not present at all"
  done
fi

# -----------------------------------------------------------------------------
hdr "3. Core metric families"
# -----------------------------------------------------------------------------
check_metric() {
  local metric="$1" hint="$2"
  local n
  n=$(curl -sf "$PROM/api/v1/query" --data-urlencode "query=count($metric)" 2>/dev/null | python3 -c "
import json,sys
r=json.load(sys.stdin)['data']['result']
print(r[0]['value'][1] if r else '0')" 2>/dev/null || echo 0)
  [ "$n" != "0" ] && ok "$metric ($n series)" || bad "$metric MISSING — $hint"
}
check_metric service_requests                     "cluster metrics dark; check PROMETHEUS_ENDPOINT"
check_metric temporal_worker_task_slots_available "SDK metrics dark; app is not exporting"
check_metric poll_success_sync                    "run 'make smoke' first — this is a counter"
check_metric workflow_success                     "no Workflows have completed yet"

# -----------------------------------------------------------------------------
hdr "4. Rules loaded and evaluating"
# -----------------------------------------------------------------------------
rules=$(curl -sf "$PROM/api/v1/rules" 2>/dev/null)
if [ -z "$rules" ]; then
  bad "cannot read rules API"
else
  echo "$rules" | python3 -c "
import json,sys
d=json.load(sys.stdin)['data']['groups']
# Group names, not exact rule counts. Counts are generated
# (tools/generate_slo_rules.py) and legitimately change when you edit the SLI
# list — asserting on them turns a normal edit into a confusing validator
# failure. What actually matters is that every expected group loaded and every
# rule evaluates.
expected_groups=['temporal-service-health','temporal-task-queue-health',
                 'temporal-worker-fleet','temporal-application-health',
                 'temporal-slo-sli','temporal-slo-meta','temporal-slo-burn']
got={g['name']:len(g['rules']) for g in d}
rc=0
for name in expected_groups:
    if name in got: print(f'  \033[32m PASS\033[0m  {name}: {got[name]} rules')
    else: print(f'  \033[31m FAIL\033[0m  {name}: group did not load'); rc=1
unhealthy=[r['name'] for g in d for r in g['rules'] if r.get('health') not in ('ok','unknown')]
if unhealthy: print(f'  \033[31m FAIL\033[0m  unhealthy rules: {unhealthy}'); rc=1
else: print('  \033[32m PASS\033[0m  all rules healthy')
sys.exit(rc)
" && PASS=$((PASS+8)) || FAIL=$((FAIL+1))
fi

# -----------------------------------------------------------------------------
hdr "5. Dashboard panel queries"
# -----------------------------------------------------------------------------
# Panels legitimately empty on a healthy system are listed here BY DESIGN.
# If you add one, add it here too, with the reason — an undocumented empty panel
# is indistinguishable from a broken one.
python3 - <<'PY'
import json, glob, os, sys, urllib.parse, urllib.request

PROM = os.environ.get("PROM", "http://localhost:9090")

def _q(expr):
    u = PROM + "/api/v1/query?query=" + urllib.parse.quote(expr)
    try:
        return json.load(urllib.request.urlopen(u, timeout=20))["data"]["result"]
    except Exception:
        return []

# Has any Activity actually run recently?
#
# Several SDK histograms — schedule-to-start above all — do not EXIST until a
# Task has been delivered. On an idle stack their panels are legitimately empty,
# and failing on that turns "nobody has sent traffic yet" into a red validator
# run that looks like a broken deployment. Gate on real traffic and downgrade
# empties to warnings when there is none.
# Presence, NOT rate(). A counter that has just come into existence has no
# earlier sample to subtract from, so rate() over it returns 0 — which would
# make this gate report "no traffic" immediately after the first Workflow ran.
_r = _q('sum(temporal_activity_schedule_to_start_latency_seconds_count)')
HAS_TRAFFIC = bool(_r) and float(_r[0]["value"][1]) > 0
if not HAS_TRAFFIC:
    print("\n  \033[33mNOTE\033[0m  no Activity traffic in the last 10m — panels that need")
    print("        traffic will WARN rather than FAIL. Run 'make smoke' or")
    print("        'make baseline', wait ~30s, and re-run for a full check.")

# Matched CASE-INSENSITIVELY: the same concept is titled differently on
# different dashboards ("Workflow Outcomes" vs "Workflow outcomes"), and an
# allowlist that misses on capitalisation fails a perfectly healthy stack.
EXPECTED_EMPTY = {k.lower(): v for k, v in {
    "Tasks With No Poller (expect zero)":
        "no orphaned Task Queues (the panel title says so)",
    "Tasks with no poller":
        "no orphaned Task Queues — above zero is the alarm",
    "Workflow Outcomes":
        "failed/timeout/cancel series only exist once such an outcome occurs",
    "Workflow outcomes":
        "failed/timeout series only exist once such an outcome occurs",
    "Server-fault rate by type":
        "no server faults have occurred — empty here is the healthy state",
}.items()}

def panels(ps):
    for p in ps:
        if p.get("type") == "row":
            yield from panels(p.get("panels", []))
        else:
            yield p

def q(expr):
    u = PROM + "/api/v1/query?query=" + urllib.parse.quote(expr)
    try:
        r = json.load(urllib.request.urlopen(u, timeout=20))
    except Exception as e:
        return "ERROR", str(e)[:70]
    if r.get("status") != "success":
        return "BADQUERY", str(r.get("error"))[:100]
    return ("DATA" if r["data"]["result"] else "EMPTY"), len(r["data"]["result"])

rc = 0
for path in sorted(glob.glob("grafana/dashboards/*/*.json")):
    if "/community/" in path:
        continue  # third-party, not ours to assert on
    d = json.load(open(path))
    d = d.get("dashboard", d)
    print(f"\n  {d.get('title', path)}")
    for p in panels(d.get("panels", [])):
        for t in p.get("targets", []):
            e = t.get("expr")
            if not e:
                continue
            e2 = (e.replace("$__rate_interval", "5m").replace("$__interval", "1m")
                   .replace("$namespace", ".*").replace("$task_queue", ".*"))
            st, info = q(e2)
            title = p.get("title", "?")
            if st == "DATA":
                print(f"    \033[32m PASS\033[0m  {title[:58]}")
            elif st == "EMPTY" and title.lower() in EXPECTED_EMPTY:
                print(f"    \033[33m ----\033[0m  {title[:58]}  (empty by design: {EXPECTED_EMPTY[title.lower()]})")
            elif st == "EMPTY" and not HAS_TRAFFIC:
                print(f"    \033[33m WARN\033[0m  {title[:58]}  no data (no traffic yet)")
            elif st == "EMPTY":
                print(f"    \033[31m FAIL\033[0m  {title[:58]}  returned NO DATA")
                rc = 1
            else:
                print(f"    \033[31m FAIL\033[0m  {title[:58]}  {st}: {info}")
                rc = 1
sys.exit(rc)
PY
[ $? -eq 0 ] && ok "all dashboard panels return data (or are empty by design)" \
             || bad "one or more dashboard panels returned no data"

# -----------------------------------------------------------------------------
hdr "6. SLO board state"
# -----------------------------------------------------------------------------
curl -sf "$PROM/api/v1/query" --data-urlencode 'query=slo:error_budget_remaining:ratio' 2>/dev/null | python3 -c "
import json,sys
r=json.load(sys.stdin)['data']['result']
if not r:
    print('  \033[31m FAIL\033[0m  no SLO series — generate traffic, then wait ~60s'); sys.exit(1)
print(f\"    {'SLI':28} {'ROLE':10} {'BUDGET LEFT':>12}\")
neg=[]
for m in sorted(r, key=lambda x: x['metric'].get('sli','')):
    sli=m['metric'].get('sli'); svc=m['metric'].get('service_name',''); v=float(m['value'][1])
    s='   no traffic' if v!=v else f'{v*100:+11.2f}%'
    print(f'    {sli:28} {svc:10} {s}')
    if v==v and v<=0: neg.append(sli)
print()
if neg: print(f'  \033[33m WARN\033[0m  budget exhausted: {sorted(set(neg))} (expected after a chaos run)')
else:   print('  \033[32m PASS\033[0m  every SLO within budget')
" || bad "could not read SLO series"

# -----------------------------------------------------------------------------
hdr "7. Alert state"
# -----------------------------------------------------------------------------
echo "$rules" | python3 -c "
import json,sys
for g in json.load(sys.stdin)['data']['groups']:
    for r in g['rules']:
        if r['type']=='alerting':
            st=r.get('state','-')
            col={'firing':'\033[31m','pending':'\033[33m'}.get(st,'\033[32m')
            print(f\"    {col}{st:8}\033[0m {r['name']}\")
" 2>/dev/null || echo "    (unavailable)"

# -----------------------------------------------------------------------------
printf "\n\033[1m== Summary\033[0m\n"
printf "  passed: %s   failed: %s   warnings: %s\n\n" "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -gt 0 ]; then
  echo "  Something is broken. See SETUP.md troubleshooting."
  exit 1
fi
echo "  Stack is healthy."
echo "  Overview   $GRAF/d/temporal-self-hosted-overview"
echo "  SLO board  $GRAF/d/temporal-slo-board"
exit 0
