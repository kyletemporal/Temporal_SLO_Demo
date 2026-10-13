#!/usr/bin/env bash
#
# Worker scale benchmark — how much does one Worker actually buy you?
#
# Holds a FIXED offered load and steps the Worker count, measuring sustained
# throughput, queue latency and slot pressure at each step. The output is a CSV
# and a markdown table you can paste into a capacity discussion.
#
# WHY THIS EXISTS
#
# RECOMMENDATIONS.md §4 documents a decision tree for a rising
# schedule-to-start: too few Workers, or each Worker configured too small. Both
# look IDENTICAL on the schedule-to-start panel, and the tree splits on host CPU
# to tell them apart. That is the correct diagnosis and it is still a diagnosis
# made during an incident, from a dashboard, under pressure.
#
# This answers the same question in advance and in numbers. Adding a Worker
# either raises sustained throughput or it does not; if it does not, the
# bottleneck is per-Worker concurrency (or the server) and no amount of
# horizontal scaling will help. Knowing which BEFORE the incident is the point.
#
# WHAT IT MEASURES, AND WHAT IT DOES NOT
#
# Measures: whether this fleet, on this hardware, converts added Workers into
# added throughput, and where it stops doing so.
#
# Does NOT measure: production capacity. These are laptop numbers from a
# single-container Temporal running all four roles beside the load generator,
# Prometheus, Grafana, Loki, Tempo and Pyroscope. Read the SHAPE of the curve —
# where it bends — not the absolute figures. The methodology transfers; the
# numbers do not.
#
# Usage:
#   scripts/scale-benchmark.sh                          # 1,2,4 workers @ 40/s
#   WORKERS="1 2 4 8" RATE=60 scripts/scale-benchmark.sh
#   DURATION=5m WARMUP=90 scripts/scale-benchmark.sh    # longer, more stable
#
set -euo pipefail

cd "$(dirname "$0")/.."

WORKERS="${WORKERS:-1 2 4}"
RATE="${RATE:-40}"
DURATION="${DURATION:-3m}"
ACTIVITY_DELAY_MS="${ACTIVITY_DELAY_MS:-50}"
PROM="${PROM:-http://localhost:9090}"
OUTDIR="${OUTDIR:-./scale-results}"

# WARMUP is discarded from the front of every step's measurement window.
#
# It is not padding. Three things need time to settle after a scale change and
# all three would otherwise corrupt the numbers: Prometheus's dns_sd has a 10s
# refresh so new Workers are not yet scraped, rate() over a partially-filled
# window under-reports, and a fleet that was behind spends the first seconds
# draining the backlog it accumulated at the previous step — which reads as
# throughput ABOVE the offered rate.
WARMUP="${WARMUP:-60}"

# Duration of the measurement window, in seconds: the step minus the warmup.
dur_to_s() {
  case "$1" in
    *m) echo $(( ${1%m} * 60 )) ;;
    *s) echo "${1%s}" ;;
    *)  echo "$1" ;;
  esac
}
STEP_S=$(dur_to_s "$DURATION")
WINDOW_S=$(( STEP_S - WARMUP ))
if [ "$WINDOW_S" -lt 60 ]; then
  echo "ERROR: DURATION ($DURATION) minus WARMUP ($WARMUP s) leaves ${WINDOW_S}s." >&2
  echo "A window under 60s is dominated by scrape jitter — raise DURATION." >&2
  exit 1
fi

mkdir -p "$OUTDIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
CSV="$OUTDIR/scale-$STAMP.csv"
MD="$OUTDIR/scale-$STAMP.md"

# ---------------------------------------------------------------------------
# PromQL helpers
# ---------------------------------------------------------------------------

# Query and return a single scalar, or "" when there is no data.
#
# Note `|| true` on the curl: with `set -o pipefail`, a curl whose stdout is
# closed early by the consumer exits non-zero on SIGPIPE and takes the whole
# script down. The same failure mode bit validate.sh in three places and is
# documented there.
prom() {
  local q="$1"
  local encoded
  encoded=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$q")
  curl -s --max-time 20 "$PROM/api/v1/query?query=$encoded" 2>/dev/null \
    | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)["data"]["result"]
except Exception:
    print(""); sys.exit()
print(r[0]["value"][1] if r else "")
' || true
}

fmt() {
  python3 -c '
import sys
v = sys.argv[1]
if v in ("", "NaN", "+Inf", "-Inf"):
    print("n/a")
else:
    print(f"{float(v):.{int(sys.argv[2])}f}")
' "$1" "$2"
}

# Exclude Temporal's own internal namespace and Workers. Without this every
# aggregate here is polluted by system activity that has nothing to do with the
# offered load — the same exclusion every dashboard in this repo applies.
TEN='namespace!~"temporal_system|system|_unknown_"'

echo "workers,offered_rate,throughput_wf_s,activities_s,sched_to_start_p95_s,sched_to_start_p99_s,min_slots_available,sync_match_rate,poll_success_rate,cpu_per_worker" > "$CSV"

echo
echo "================================================================"
echo "  Worker scale benchmark"
echo "================================================================"
echo "  worker counts : $WORKERS"
echo "  offered load  : ${RATE}/s"
echo "  step duration : $DURATION  (measuring the last ${WINDOW_S}s)"
echo "  activity delay: ${ACTIVITY_DELAY_MS}ms"
echo "  results       : $CSV"
echo "================================================================"
echo

for n in $WORKERS; do
  echo "--- scaling to $n worker(s) ---"

  # `rm -sf` before scaling up, not just `--scale`.
  #
  # `--scale worker=1` leaves the surplus containers STOPPED rather than
  # removed, and a later compose command restarts them — measured once as 11
  # running Workers during a step that was supposed to have 1, which silently
  # invalidated every row after it. Removing them is the only reliable way down.
  docker compose rm -sf worker >/dev/null 2>&1 || true
  docker compose up -d --scale worker="$n" --no-recreate worker >/dev/null

  echo "    running ${DURATION} at ${RATE}/s ..."
  docker compose run --rm \
    -e RATE="$RATE" -e DURATION="$DURATION" -e ACTIVITY_DELAY_MS="$ACTIVITY_DELAY_MS" \
    k6 run /scripts/09-scale-sweep.js >"$OUTDIR/k6-$n-$STAMP.log" 2>&1 || true

  # One scrape interval of slack so the final samples of the run are in
  # Prometheus before it is queried.
  sleep 15

  W="[${WINDOW_S}s]"

  # Workers actually scraped, not the number requested. These diverge when
  # dns_sd has not caught up or a container failed to start, and trusting the
  # requested count is how a benchmark reports per-worker throughput against a
  # fleet size that never existed.
  actual_workers=$(prom "count(up{job=\"temporal-sdk-worker\"} == 1)")

  throughput=$(prom "sum(rate(workflow_success{${TEN}}${W}))")
  # _seconds_count, NOT _count. Both names appear in the metric index, but
  # temporal_activity_execution_latency_count has ZERO live series — it is a
  # stale artefact. The Go SDK emits durations in seconds and the naming scope
  # adds the suffix, so the histogram's count is _seconds_count. Measured: the
  # unsuffixed name returned n/a for a whole benchmark run.
  activities=$(prom "sum(rate(temporal_activity_execution_latency_seconds_count{${TEN}}${W}))")

  s2s_p95=$(prom "histogram_quantile(0.95, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{${TEN}}${W})))")
  s2s_p99=$(prom "histogram_quantile(0.99, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{${TEN}}${W})))")

  # The MINIMUM over the window, not the average.
  #
  # Slot exhaustion is a transient that the mean hides completely: a fleet that
  # hits zero available slots for two seconds in every ten has a comfortable
  # average and is dropping throughput on the floor. The minimum is what
  # separates "slot-bound" from "worker-bound", which is the whole question.
  # TWO corrections here, both found by a run that reported n/a and then 8110:
  #
  #  1. SUBQUERY SYNTAX. min_over_time() over an aggregation needs [Xs:Ys], not
  #     [Xs] — a plain range on a non-selector is a parse error, and prom()
  #     turns any error into an empty string, which prints as a harmless-looking
  #     "n/a" rather than as a failure.
  #
  #  2. job FILTER. Without it this sums Temporal's OWN internal SDK workers,
  #     which run inside the server container with 1000 slots per internal task
  #     queue — the first corrected run reported 8110 free slots for a fleet
  #     that had 40. Same trap as the temporal_system namespace exclusion
  #     applied everywhere else in this repo, on a metric that has no namespace
  #     label to exclude on.
  min_slots=$(prom "min_over_time(sum(temporal_worker_task_slots_available{worker_type=\"ActivityWorker\", job=\"temporal-sdk-worker\"})[${WINDOW_S}s:15s])")

  sync_match=$(prom "sum(rate(poll_success_sync${W})) / clamp_min(sum(rate(poll_success${W})), 0.0001)")
  poll_success=$(prom "sum(rate(poll_success${W})) / clamp_min(sum(rate(poll_success${W})) + sum(rate(poll_timeouts${W})), 0.0001)")

  # Container CPU, from cAdvisor if present. Absent on this stack by default,
  # so it reports n/a rather than a zero that would read as "idle" — a
  # fabricated zero in the column that distinguishes CPU-bound from slot-bound
  # would actively mislead the diagnosis this table exists to support.
  cpu=$(prom "avg(rate(container_cpu_usage_seconds_total{name=~\".*worker.*\"}${W}))")

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "${actual_workers:-$n}" "$RATE" \
    "$(fmt "$throughput" 2)" "$(fmt "$activities" 2)" \
    "$(fmt "$s2s_p95" 4)" "$(fmt "$s2s_p99" 4)" \
    "$(fmt "$min_slots" 0)" "$(fmt "$sync_match" 4)" \
    "$(fmt "$poll_success" 4)" "$(fmt "$cpu" 3)" >> "$CSV"

  echo "    workers=${actual_workers:-?}  throughput=$(fmt "$throughput" 2)/s  s2s_p95=$(fmt "$s2s_p95" 4)s  min_slots=$(fmt "$min_slots" 0)"
  echo
done

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
python3 - "$CSV" "$MD" "$RATE" "$DURATION" "$ACTIVITY_DELAY_MS" "$WINDOW_S" <<'PY'
import csv, sys

csv_path, md_path, rate, duration, delay, window = sys.argv[1:7]
rows = list(csv.DictReader(open(csv_path)))

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

lines = [
    "# Worker scale benchmark",
    "",
    f"- Offered load: **{rate}/s** for **{duration}** per step "
    f"(last **{window}s** measured)",
    f"- Activity delay: **{delay}ms** — one activity slot sustains "
    f"~{1000 / max(int(delay), 1):.0f} activities/sec",
    "",
    "Laptop numbers from a single-container Temporal running all four roles "
    "beside the load generator and the whole observability stack. **Read where "
    "the curve bends, not the absolute values.**",
    "",
    "| Workers | Offered /s | Throughput wf/s | Activities/s | S2S p95 (s) | "
    "S2S p99 (s) | Min slots free | Sync match | Poll success | Efficiency |",
    "|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
]

base_per_worker = None
for r in rows:
    w, tp = num(r["workers"]), num(r["throughput_wf_s"])

    # Efficiency = throughput per worker, normalised to the first step. 1.00
    # means this step converted added Workers into added throughput as well as
    # the first one did; 0.50 means each added Worker is buying half as much.
    eff = "n/a"
    if w and tp and w > 0:
        per = tp / w
        if base_per_worker is None:
            base_per_worker = per
            eff = "1.00 (base)"
        elif base_per_worker > 0:
            eff = f"{per / base_per_worker:.2f}"

    lines.append(
        f"| {r['workers']} | {r['offered_rate']} | {r['throughput_wf_s']} | "
        f"{r['activities_s']} | {r['sched_to_start_p95_s']} | "
        f"{r['sched_to_start_p99_s']} | {r['min_slots_available']} | "
        f"{r['sync_match_rate']} | {r['poll_success_rate']} | {eff} |"
    )

lines += ["", "## Reading it", ""]

offered = num(rate)
first, last = (rows[0], rows[-1]) if rows else (None, None)

if first and last:
    tp_first, tp_last = num(first["throughput_wf_s"]), num(last["throughput_wf_s"])
    slots_last = num(last["min_slots_available"])
    s2s_first, s2s_last = num(first["sched_to_start_p95_s"]), num(last["sched_to_start_p95_s"])

    # The offered load has to exceed what the smallest fleet can absorb, or the
    # sweep measures nothing at all. Say so plainly rather than presenting a
    # flat line as a finding.
    if tp_first and offered and tp_first >= offered * 0.95:
        lines += [
            f"**This run is inconclusive.** One Worker already sustained "
            f"{tp_first:.1f}/s against an offered {offered:.0f}/s, so the fleet was "
            "never the constraint and every later step had nothing left to improve. "
            "Re-run with a higher `RATE` (or a larger `ACTIVITY_DELAY_MS`) until the "
            "first row falls short of the offered load — a sweep that never saturates "
            "produces a flat table and no information.",
            "",
        ]
    elif tp_first and tp_last and tp_last > tp_first * 1.3:
        lines += [
            f"**Throughput scaled with the fleet** — {tp_first:.1f}/s → {tp_last:.1f}/s. "
            "Adding Workers bought real capacity, so for this workload horizontal "
            "scaling is the correct lever and an autoscaler on schedule-to-start "
            "(`aws/k8s/worker/hpa.yaml`) will do useful work.",
            "",
        ]
    elif tp_first and tp_last:
        lines += [
            f"**Throughput did NOT scale** — {tp_first:.1f}/s → {tp_last:.1f}/s across "
            "the sweep. Added Workers bought little or nothing, which means the "
            "bottleneck is not fleet size. Check, in order: per-Worker concurrency "
            "(`MAX_CONCURRENT_ACTIVITIES`), then the Temporal server, then the load "
            "generator itself. **An autoscaler would make this worse** — more Workers "
            "polling a server that is already the constraint adds poll load without "
            "adding capacity (that is scenario 8, poller flood).",
            "",
        ]

    if slots_last is not None and slots_last <= 0:
        lines += [
            "**Slots hit zero.** At the largest fleet there was at least one moment "
            "with no activity slot free anywhere, so the fleet was *slot-bound* rather "
            "than Worker-bound at that point. Raise `MAX_CONCURRENT_ACTIVITIES` before "
            "adding Workers — this is the branch of the RECOMMENDATIONS.md §4 tree that "
            "adding Workers does not fix.",
            "",
        ]

    # Compare before asserting a direction. An earlier version hardcoded "fell"
    # and duly reported that latency "fell from 0.426s to 0.824s" — a dashboard
    # that narrates the opposite of its own data is worse than one with no prose
    # at all, because the prose is what people read.
    # Whether a FALLING schedule-to-start is good news depends on the
    # throughput column, so this branch has to know what that one concluded.
    #
    # Measured 2026-09-28 at 40/s: throughput flat at ~36/s while S2S fell
    # 0.289s -> 0.171s. The two sections were independent, so the report said
    # "an autoscaler would make this worse" and "schedule-to-start is the right
    # metric to autoscale on" four lines apart, in the same document. Both
    # sentences were individually defensible and the pair was worthless.
    scaled = bool(tp_first and tp_last and tp_last > tp_first * 1.3)

    if s2s_first is not None and s2s_last is not None and max(s2s_first, s2s_last) > 0.05:
        if s2s_last < s2s_first * 0.8 and scaled:
            lines += [
                f"**Schedule-to-start fell from {s2s_first:.3f}s to {s2s_last:.3f}s.** Queue "
                "wait improved as the fleet grew, and throughput rose with it — together that "
                "is the evidence that schedule-to-start is the right metric to autoscale on "
                "for this workload (`aws/k8s/worker/hpa.yaml`).",
                "",
            ]
        elif s2s_last < s2s_first * 0.8:
            lines += [
                f"**Schedule-to-start fell from {s2s_first:.3f}s to {s2s_last:.3f}s — but "
                "throughput did not move.** The fleet got better at picking work UP without "
                "getting more work DONE. Queue wait is the easy thing for an added Worker to "
                "improve; it is not evidence of added capacity, and on its own it is not a "
                "reason to autoscale. An HPA on schedule-to-start would scale up, watch the "
                "signal recover, scale back down, and leave throughput exactly where it "
                "started. Look at sync match rate and poll success in the same rows before "
                "concluding anything about capacity.",
                "",
            ]
        elif s2s_last > s2s_first * 1.2:
            lines += [
                f"**Schedule-to-start ROSE from {s2s_first:.3f}s to {s2s_last:.3f}s** as Workers "
                "were added. Queue wait getting worse with a bigger fleet means the added "
                "Workers are contending for something rather than draining the queue — the "
                "constraint is downstream of the fleet (server, persistence, or the load "
                "generator). An HPA scaling on this signal would scale UP in response, make it "
                "worse, and scale up again.",
                "",
            ]
        else:
            lines += [
                f"**Schedule-to-start was flat** ({s2s_first:.3f}s → {s2s_last:.3f}s). Fleet size "
                "is not what is setting queue wait here.",
                "",
            ]

lines += [
    "### What each column tells you",
    "",
    "| Column | Reads as |",
    "|---|---|",
    "| Throughput below offered | The fleet cannot keep up. The gap is backlog being built. |",
    "| S2S p95 rising | Tasks are waiting for a poller. The first symptom of saturation. |",
    "| Min slots free at 0 | Slot-bound. More Workers will not help; raise concurrency. |",
    "| Min slots free high, S2S high | Worker-bound or server-bound, NOT slot-bound. |",
    "| Sync match falling | The queue stopped draining instantly — tasks now round-trip persistence. |",
    "| Poll success falling | More pollers than work. Over-provisioned (see scenario 8). |",
    "| Efficiency below ~0.8 | Diminishing returns. Near the knee of the curve. |",
    "",
    "Full methodology and the capacity arithmetic: `docs/SCALE-TESTING.md`.",
]

open(md_path, "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
PY

echo
echo "CSV      : $CSV"
echo "Report   : $MD"
echo
echo "Reset the fleet to one Worker with:  docker compose rm -sf worker && docker compose up -d worker"
