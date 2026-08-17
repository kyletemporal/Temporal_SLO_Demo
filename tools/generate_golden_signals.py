#!/usr/bin/env python3
"""RED + Saturation (four golden signals) dashboard with SLOs on top."""
import json, sys

import pathlib
OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent / "production/grafana/dashboards/temporal-golden-signals.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
DS_LOKI = {"type": "loki", "uid": "loki"}

# Status palette — reserved for state, never reused as a series colour.
GOOD, SERIOUS, CRITICAL = "#0ca30c", "#ec835a", "#d03b3b"
# Categorical, dark steps, fixed order. Validated: all adjacent pairs pass
# CVD/normal-vision/contrast on Grafana's dark surface (#181b1f).
C1, C2, C3, C4, C5 = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
# Sequential blue for ORDERED magnitude (quantiles), bounded per the ordinal
# rule so no step recedes into the dark surface.
S_LO, S_MID, S_HI = "#9ec5f4", "#5598e7", "#2a78d6"

NLP = 'operation!~"Poll.*|GetTaskQueueUserData|ListNexusEndpoints"'
CF = ('error_type!~"serviceerror_(Canceled|NotFound|NamespaceNotFound'
      '|AlreadyExist.*|InvalidArgument|FailedPrecondition'
      '|WorkflowExecutionAlreadyStarted|QueryFailed)"')
TEN = 'namespace!~"temporal_system|system|_unknown_"'

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def tgt(expr, legend=None, ref="A", instant=False):
    t = {"datasource": DS, "editorMode": "code", "expr": expr,
         "range": not instant, "instant": instant, "refId": ref}
    if legend: t["legendFormat"] = legend
    return t

def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "id": nid(),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}

def color_override(name, hexv):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexv}}]}

def ts(title, gp, targets, unit, desc, overrides=None, thr=None,
       decimals=None, minv=None, maxv=None, legend_mode="list",
       legend_place="bottom", calcs=None, fill=8, scale=None):
    d = {"color": {"mode": "thresholds" if thr else "palette-classic"},
         "unit": unit,
         "custom": {
             # Thin marks, recessive fills — the data, not the ink.
             "lineWidth": 2, "fillOpacity": fill, "showPoints": "never",
             "spanNulls": False, "gradientMode": "opacity",
             "lineInterpolation": "smooth",
             "axisSoftMin": 0,
         }}
    if thr: d["thresholds"] = thr
    if decimals is not None: d["decimals"] = decimals
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    if scale: d["custom"]["scaleDistribution"] = scale
    return {"type": "timeseries", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": legend_mode, "placement": legend_place,
                                   "showLegend": True,
                                   "calcs": calcs or []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def stat(title, gp, targets, unit, desc, thr, decimals=None, graph="area",
         text="auto", color_mode="value", novalue=None, minv=None, maxv=None):
    d = {"color": {"mode": "thresholds"}, "unit": unit, "thresholds": thr}
    if decimals is not None: d["decimals"] = decimals
    if novalue: d["noValue"] = novalue
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    return {"type": "stat", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": graph,
                        "textMode": text, "justifyMode": "auto", "wideLayout": True}}

P = []

# =========================================================================
# SERVICE LEVEL — the promise, on top, because it is the only row that says
# whether any of the signals below actually matter to a customer yet.
# =========================================================================
P.append(row("Service level — are we keeping the promise?", 0))

P.append(stat("SLOs in breach", {"h": 6, "w": 4, "x": 0, "y": 1},
    [tgt("count(slo:error_budget_remaining:ratio <= 0) or vector(0)", instant=True)],
    "none", "Zero means every error budget still has room. Read this first; if it is zero the signals below are informational, not urgent.",
    {"mode": "absolute", "steps": [{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 1}]},
    graph="none", text="value", color_mode="background"))

wall = stat("Error budget remaining", {"h": 6, "w": 12, "x": 4, "y": 1},
    [tgt("clamp_min(slo:error_budget_remaining:ratio, -1)", legend="{{sli}} {{service_name}}{{namespace}}", instant=True)],
    "percentunit",
    "One tile per SLO. Saturates at -100%: past that the budget is gone and burn rate is the number that matters.\n\n"
    "Read the value, not only the colour — green vs orange is near-indistinguishable under protanopia, so the signed percentage carries the meaning.",
    {"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                   {"color": SERIOUS, "value": 0.0001},
                                   {"color": GOOD, "value": 0.25}]},
    decimals=0, graph="none", text="value_and_name", color_mode="background",
    novalue="no traffic", minv=-1, maxv=1)
P.append(wall)

P.append(ts("Burn rate (1h)", {"h": 6, "w": 8, "x": 16, "y": 1},
    [tgt("slo:burn_rate:ratio_rate1h", legend="{{sli}} {{namespace}}")],
    "none",
    "Multiples of sustainable budget spend. 1x exhausts the budget exactly at the window's end; 14.4x is the fast-burn page threshold.\n\nsymlog axis: a real incident reaches 80x+, which on a linear axis flattens the 1x and 14.4x decision lines into the baseline.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 1},
                                       {"color": CRITICAL, "value": 14.4}]},
    scale={"type": "symlog", "log": 10, "linearThreshold": 1}, fill=0))

# =========================================================================
# R — RATE
# =========================================================================
P.append(row("R — Rate  ·  how much work is arriving", 7))

P.append(stat("Cluster request rate", {"h": 7, "w": 4, "x": 0, "y": 8},
    [tgt('sum(rate(service_requests{service_name="frontend"}[$__rate_interval]))')],
    "reqps", "All Frontend gRPC calls, long-polls included — this is traffic, and a Worker polling IS traffic. A sudden drop here with no error rise usually means callers stopped, not that you broke.",
    {"mode": "absolute", "steps": [{"color": C1, "value": None}]}, decimals=1))

P.append(ts("Frontend request rate by operation", {"h": 7, "w": 10, "x": 4, "y": 8},
    [tgt(f'topk(6, sum by (operation) (rate(service_requests{{service_name="frontend"}}[$__rate_interval])))',
         legend="{{operation}}")],
    "reqps",
    "Top 6 operations. PollWorkflowTaskQueue and PollActivityTaskQueue normally dominate — that is Workers waiting for work, not load. Watch the shape of StartWorkflowExecution for real demand.",
    legend_mode="table", legend_place="right", calcs=["lastNotNull"]))

P.append(ts("Workflow throughput", {"h": 7, "w": 10, "x": 14, "y": 8},
    [tgt(f'sum(rate(service_requests{{service_name="frontend", operation="StartWorkflowExecution"}}[$__rate_interval]))', legend="started"),
     tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval]))', legend="completed", ref="B")],
    "reqps",
    "Started vs completed. A persistent gap means work is accumulating — the single clearest early sign of a saturated Worker fleet, and it shows up here long before anything errors.",
    overrides=[color_override("started", C1), color_override("completed", C3)],
    legend_mode="list", calcs=[]))

# =========================================================================
# E — ERRORS
# =========================================================================
P.append(row("E — Errors  ·  what is failing, and whose fault is it", 15))

P.append(stat("Frontend error ratio", {"h": 7, "w": 4, "x": 0, "y": 16},
    [tgt(f'''(
  sum(rate(service_error_with_type{{service_name="frontend", {CF}}}[$__rate_interval]))
  or sum(rate(service_requests{{service_name="frontend"}}[$__rate_interval])) * 0
)
/ clamp_min(sum(rate(service_requests{{service_name="frontend"}}[$__rate_interval])), 0.001)''')],
    "percentunit",
    "Server-fault ratio. Client-caused errors (Canceled, NotFound, InvalidArgument) are excluded — counting them measures your callers' behaviour, not your availability.",
    {"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                   {"color": SERIOUS, "value": 0.001},
                                   {"color": CRITICAL, "value": 0.01}]},
    decimals=3, color_mode="background"))

P.append(ts("Server-fault rate by type", {"h": 7, "w": 10, "x": 4, "y": 16},
    [tgt(f'sum by (error_type) (rate(service_error_with_type{{{CF}}}[$__rate_interval]))',
         legend="{{error_type}}")],
    "reqps",
    "Faults that are yours. ResourceExhausted points at rate limiting or datastore pressure; Internal/Unavailable points at cluster health. Empty here on a healthy cluster is correct, not broken.",
    legend_mode="table", legend_place="right", calcs=["lastNotNull"]))

P.append(ts("Workflow outcomes", {"h": 7, "w": 10, "x": 14, "y": 16},
    [tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval]))', legend="success"),
     tgt(f'sum(rate(workflow_failed{{{TEN}}}[$__rate_interval]))', legend="failed", ref="B"),
     tgt(f'sum(rate(workflow_timeout{{{TEN}}}[$__rate_interval]))', legend="timeout", ref="C")],
    "reqps",
    "The application signal, not the cluster signal. Activity failures absorbed by retries are HEALTHY and do not appear here.\n\nWatch timeout specifically: a saturated fleet produces timeouts, not failures. Measured under a backlog storm, failed sat at 0.02/s while timeout hit 24.6/s.",
    overrides=[color_override("success", GOOD),
               color_override("failed", CRITICAL),
               color_override("timeout", SERIOUS)],
    legend_mode="list", fill=18))

# =========================================================================
# D — DURATION
# =========================================================================
P.append(row("D — Duration  ·  how long it takes", 23))

P.append(ts("Frontend latency (long-polls excluded)", {"h": 7, "w": 8, "x": 0, "y": 24},
    [tgt(f'histogram_quantile(0.50, sum by (le) (rate(service_latency_bucket{{service_name="frontend", {NLP}}}[$__rate_interval])))', legend="p50"),
     tgt(f'histogram_quantile(0.95, sum by (le) (rate(service_latency_bucket{{service_name="frontend", {NLP}}}[$__rate_interval])))', legend="p95", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(service_latency_bucket{{service_name="frontend", {NLP}}}[$__rate_interval])))', legend="p99", ref="C")],
    "s",
    "Long-poll operations are EXCLUDED. They block up to 60s by design and are the highest-volume Frontend calls; including them makes this panel measure Worker idle time. Measured: 95.9% under 500ms with polls in, 100% with them out.",
    overrides=[color_override("p50", S_LO), color_override("p95", S_MID), color_override("p99", S_HI)],
    legend_mode="list", fill=0))

P.append(ts("Persistence P95 by operation", {"h": 7, "w": 8, "x": 8, "y": 24},
    [tgt('topk(5, histogram_quantile(0.95, sum by (operation, le) (rate(persistence_latency_bucket[$__rate_interval]))))',
         legend="{{operation}}")],
    "s",
    "Check this BEFORE scaling any Temporal service. The datastore is upstream of nearly every Temporal latency symptom — most self-hosted incidents are persistence incidents wearing a different hat.",
    legend_mode="table", legend_place="right", calcs=["lastNotNull"]))

P.append(ts("Schedule-to-start P99", {"h": 7, "w": 8, "x": 16, "y": 24},
    [tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', legend="activity"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_workflow_task_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', legend="workflow task", ref="B")],
    "s",
    "Queue wait, not execution time — how long work sat before a Worker picked it up. The customer-visible symptom of an undersized fleet.\n\nIf this is flat during a known backlog, ScheduleToStartTimeout is set in Activity Options and is truncating the metric.",
    overrides=[color_override("activity", C1), color_override("workflow task", C2)],
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.2}]},
    legend_mode="list", fill=0))

# =========================================================================
# S — SATURATION
# =========================================================================
P.append(row("S — Saturation  ·  how close to the limit", 31))

P.append(ts("Worker slot utilisation", {"h": 7, "w": 8, "x": 0, "y": 32},
    [tgt(f'''sum by (task_queue) (temporal_worker_task_slots_used{{{TEN}}})
/ clamp_min(
    sum by (task_queue) (temporal_worker_task_slots_used{{{TEN}}})
  + sum by (task_queue) (temporal_worker_task_slots_available{{{TEN}}}), 1)''',
         legend="{{task_queue}}")],
    "percentunit",
    "Fraction of Worker capacity in use. At 100% with LOW host CPU, raise MaxConcurrentActivityExecutionSize — do not add Workers. At 100% with HIGH host CPU, add Workers.\n\nThat discriminator is host CPU, which is NOT a Temporal metric: you need node_exporter or cAdvisor alongside this or the two cases are indistinguishable.",
    minv=0, maxv=1, decimals=2,
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.8},
                                       {"color": CRITICAL, "value": 0.99}]},
    legend_mode="table", legend_place="right", calcs=["lastNotNull", "max"]))

P.append(ts("Sync match rate", {"h": 7, "w": 8, "x": 8, "y": 32},
    [tgt(f'''sum by (namespace) (rate(poll_success_sync{{{TEN}}}[$__rate_interval]))
/ clamp_min(sum by (namespace) (rate(poll_success{{{TEN}}}[$__rate_interval])), 1)''',
         legend="{{namespace}}")],
    "percentunit",
    "Share of Tasks handed straight to a waiting Worker instead of being written to the datastore first. High is good and cheap; a fall means Workers were not ready when work arrived.\n\nNear-zero on an IDLE queue is meaningless, not bad — the ratio needs real poll traffic to mean anything.",
    minv=0, maxv=1, decimals=3,
    thr={"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                       {"color": SERIOUS, "value": 0.9},
                                       {"color": GOOD, "value": 0.95}]},
    legend_mode="list", fill=0))

P.append(ts("Tasks with no poller", {"h": 7, "w": 8, "x": 16, "y": 32},
    [tgt(f'sum by (taskqueue) (rate(no_poller_tasks{{{TEN}}}[$__rate_interval]))',
         legend="{{taskqueue}}")],
    "reqps",
    "Work queued for a Task Queue nobody is polling. The one signal here with no false-positive mode — anything above zero is real.\n\nUsually a Task Queue name mismatch or a Worker fleet that is entirely down. Nothing fails; the work just never runs. Scaling Workers will not fix a name mismatch.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": CRITICAL, "value": 0.0001}]},
    legend_mode="list", fill=18))

# =========================================================================
# L — LOGS
#
# Logs are here because metrics structurally cannot answer "which execution?".
# Every rule in this repo refuses a workflow_id label — unbounded cardinality,
# and Temporal omits it from SDK metrics for the same reason. Logs carry it in
# the LINE, where LogQL extracts it at query time.
#
# On any server below 1.30 there is also no TemporalReportedProblems search
# attribute, so worker logs are the ONLY route from "something is stuck" to an
# execution ID.
# =========================================================================
def logs_panel(title, gp, expr, desc):
    return {"type": "logs", "title": title, "id": nid(), "datasource": DS_LOKI,
            "gridPos": gp, "description": desc,
            "targets": [{"datasource": DS_LOKI, "expr": expr, "queryType": "range", "refId": "A"}],
            "options": {"showTime": True, "wrapLogMessage": True, "prettifyLogMessage": False,
                        "enableLogDetails": True, "dedupStrategy": "none", "sortOrder": "Descending"}}

P.append(row("K — Worker cache  ·  watch, do not page", 42))

P.append(ts("Sticky cache size vs forced evictions", {"h": 8, "w": 24, "x": 0, "y": 43},
    [tgt("sum by (namespace) (temporal_sticky_cache_size)", legend="cached {{namespace}}"),
     tgt("sum by (namespace) (rate(temporal_sticky_cache_total_forced_eviction_total[$__rate_interval]))",
         legend="forced evictions/s {{namespace}}")],
    "short",
    "WATCH tier, not an alert.\n\nSticky cache size approaching WorkflowCacheSize drives forced evictions, and every eviction means the next Workflow Task replays the whole history instead of resuming from cache. That shows up as workflow task execution latency, not as an error — which is why it is worth plotting before it becomes a latency incident.",
    legend_mode="table", legend_place="bottom", fill=10))

P.append(row("L — Logs  ·  the only place an execution ID may live", 51))

P.append(ts("Log volume by service and level", {"h": 8, "w": 8, "x": 0, "y": 52},
    [tgt('sum by (service, level) (rate({project="temporal-obs-demo", level=~"error|warn"}[$__interval]))',
         legend="{{service}} {{level}}")],
    "logs",
    "Error and warning lines per second, from Loki.\n\nLevel is extracted with an ANCHORED regex on the line prefix. An unanchored match tags any line containing the word 'error' — which produced 644 phantom worker errors before it was fixed.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                      {"color": SERIOUS, "value": 0.1}]},
    legend_mode="table", legend_place="right", fill=18))
P[-1]["targets"][0]["datasource"] = DS_LOKI
P[-1]["datasource"] = DS_LOKI

P.append(logs_panel("Temporal Service errors (infra)", {"h": 8, "w": 8, "x": 8, "y": 52},
    '{project="temporal-obs-demo", service="temporal", level=~"error|warn"}',
    "Server-side errors and warnings, JSON-parsed. This is where a persistence error or a shard problem explains a metric spike that the dashboards above can only show you the shape of."))

P.append(logs_panel("Find stuck executions (workflow IDs)", {"h": 8, "w": 8, "x": 16, "y": 52},
    '{project="temporal-obs-demo", service="worker"} |~ "TMPRL1100|TMPRL1101|(?i)(non.?determin|workflow task failed|deadlock detected|panic)"',
    "THE PANEL METRICS CANNOT REPLACE.\n\nSurfaces worker log lines for non-determinism and Workflow Task failures, which carry WorkflowID and RunID.\n\nTMPRL1100 = non-determinism; TMPRL1101 = deadlock detected during workflow run (a Workflow Task taking too long, usually a blocking call in Workflow code). Both codes verified present in this stack by forcing a real NDE with `make chaos-nde`. The Loki datasource defines derived fields on both, so each ID is a link straight into the Temporal UI.\n\nOn servers below 1.30 (no TemporalReportedProblems) this is the primary route from a stuck-workflow alert to the affected executions."))


# =========================================================================
# M — WORKFLOW DURATION SLO  (from the Visibility monitor, temporal_slo_*)
#
# The only row on this board that is not derived from Prometheus counters, and
# that is the entire reason it exists. Every other panel here is built from
# metrics that describe a Workflow which ENDED. An execution that never ends
# increments none of them, so it is invisible everywhere above this row.
#
# Reproduce with `make chaos-stuck`: this row moves, nothing else does.
# =========================================================================
P.append(row("M — Workflow duration SLO  ·  the executions nothing else can see", 60))

P.append(stat("Duration compliance", {"h": 6, "w": 5, "x": 0, "y": 61},
    [tgt("slo:workflow_compliance:ratio", legend="{{workflow_type}}")],
    "percentunit",
    "Closed-in-budget / (closed-in-budget + closed-over-budget + still running past 1x budget).\n\nThe third term is why terminating a stuck Workflow does NOT improve this number: the execution moves from that term to closed-over-budget and the ratio is unchanged. Verified end to end — 5 executions moved and compliance held at 0.793336 exactly.",
    thr={"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                       {"color": SERIOUS, "value": 0.95},
                                       {"color": GOOD, "value": 0.99}]},
    decimals=2, novalue="monitor down?"))

P.append(ts("Open executions past budget", {"h": 6, "w": 10, "x": 5, "y": 61},
    [tgt('sum by (bucket) (temporal_slo_over_budget_executions)', legend="past {{bucket}}x budget")],
    "short",
    "OPEN executions that have exceeded N x their duration budget.\n\nNo alert above this row can see these. They are Running, pollers are healthy, nothing has failed and nothing is retrying — only duration is wrong, and no Prometheus counter carries duration for an open execution.\n\nMeasured during `make chaos-stuck`: 0 -> 5 at buckets 1 and 2 while no other alert fired.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 1}]},
    legend_mode="table", legend_place="bottom", fill=16))

P.append(ts("Running executions", {"h": 6, "w": 5, "x": 15, "y": 61},
    [tgt("sum by (workflow_type) (temporal_slo_running_executions)", legend="{{workflow_type}}")],
    "short",
    "Open executions by type. Deliberately NOT alertable on its own: this number scales with business volume, not with health. It is here as the denominator context for the panel to its left.",
    legend_mode="list", legend_place="bottom", fill=8))

# Two panels about whether the SIGNAL works, not whether the system is healthy.
# Both exist because this row's failure modes are silent by construction.
P.append(stat("Poll freshness", {"h": 6, "w": 2, "x": 20, "y": 61},
    [tgt("time() - max(temporal_slo_last_successful_poll_timestamp_seconds)")],
    "s",
    "Age of the newest successful Visibility poll.\n\nTHIS PANEL GUARDS THE ROW. The monitor never publishes a zero when a query fails — it leaves the previous value in place, because a 0 would read as 'nothing is over budget' during exactly the outage when that is least likely to be true. The cost is that a frozen gauge looks perfectly healthy, and this is the only thing that reveals it.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 120},
                                       {"color": CRITICAL, "value": 300}]},
    decimals=0, novalue="no polls"))

P.append(stat("Server-reported stuck detection", {"h": 6, "w": 2, "x": 22, "y": 61},
    [tgt('max(temporal_slo_stuck_detection_available{method="reported_problems"})')],
    "short",
    "1 = TemporalReportedProblems works here. 0 = it does not, so stuck_executions is NOT PUBLISHED AT ALL.\n\nAbsence rather than zero is deliberate: a stuck_executions gauge sitting at 0 on a server without the attribute is indistinguishable from a clean bill of health. Needs Server 1.30+ and, self-hosted, system.numConsecutiveWorkflowTaskProblemsToTriggerSearchAttribute — so on 1.27.4 this correctly reads 0.\n\nDuration buckets (left) work on every version and remain the primary signal.",
    thr={"mode": "absolute", "steps": [{"color": SERIOUS, "value": None},
                                       {"color": GOOD, "value": 1}]},
    decimals=0, graph="none", novalue="monitor down?"))

# =========================================================================
# N — TRACES & PROFILES
#
# Span metrics come from Tempo's generator, which derives them from SAMPLED
# traces. They are an ESTIMATE. The SDK metrics in the rows above are exact, and
# where the two disagree the SDK metrics win. These exist to break latency down
# BY SPAN — which the SDK metrics cannot do — not to serve as SLIs.
# =========================================================================
P.append(row("N — Traces & profiles  ·  where the time actually went", 69))

P.append(ts("Span latency p95 by operation (from traces)", {"h": 7, "w": 12, "x": 0, "y": 70},
    [tgt('histogram_quantile(0.95, sum by (le, span_name) (rate(traces_spanmetrics_latency_bucket[$__rate_interval])))',
         legend="{{span_name}}")],
    "s",
    "p95 per span, derived from sampled traces by Tempo's metrics generator.\n\nESTIMATE, not an SLI — sampling makes it approximate, and the SDK histograms above are exact. Its value is the breakdown: this tells you WHICH activity is slow, which no SDK metric does.",
    legend_mode="table", legend_place="right", fill=10))

P.append(ts("Span throughput by operation", {"h": 7, "w": 12, "x": 12, "y": 70},
    [tgt('sum by (span_name) (rate(traces_spanmetrics_calls_total[$__rate_interval]))', legend="{{span_name}}")],
    "reqps",
    "Calls per second per span. Useful for spotting an Activity being retried far more often than its siblings — a retry storm shows here as throughput without matching Workflow completions.\n\nTo find the executions themselves, use TraceQL:\n  { span.temporal.span.kind = \"activity\" && duration > 1s }",
    legend_mode="table", legend_place="right", fill=10))

dash = {
    "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
                  "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
    "annotations": {"list": []},
    "description": "Four golden signals (RED + Saturation) for a self-hosted Temporal Service, with SLO attainment and error budget burn on top.",
    "editable": True, "graphTooltip": 1, "links": [], "panels": P, "preload": False,
    "refresh": "30s", "schemaVersion": 39,
    "tags": ["temporal", "golden-signals", "red", "sre", "slo"],
    "templating": {"list": [{"current": {}, "hide": 0, "includeAll": False,
                             "label": "Data source", "multi": False, "name": "DS_PROMETHEUS",
                             "options": [], "query": "prometheus", "refresh": 1,
                             "regex": "", "skipUrlSync": False, "type": "datasource"}]},
    "time": {"from": "now-1h", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Temporal — Golden Signals (RED + Saturation)",
    "uid": ("temporal-golden-signals" if "demo/" in OUT else "temporal-golden-signals-prod"),  # PROD_UID: demo and production must not collide in one Grafana
    "version": 1, "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
