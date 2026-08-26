#!/usr/bin/env python3
"""Temporal — Full Overview (self-hosted).

A row-for-row rebuild of Grafana Cloud's "Temporal overview" dashboard (the
Cloud Connections integration) with every Cloud-specific thing removed and the
rest re-expressed in self-hosted metrics.

Full mapping of what was translated, replaced and dropped:
    docs/CLOUD-TO-SELFHOSTED.md

WHY A DIRECT PORT DOES NOT WORK — four independent reasons, each of which
silently produces a wrong or empty dashboard rather than an error:

1. SCHEMA. The source is `dashboard.grafana.app/v2` (Grafana 12). Grafana
   11.5.1 cannot parse it — no partial render, no message. This emits classic
   schemaVersion 39.

2. METRIC NAMESPACE. Every Cloud query reads `temporal_cloud_v1_*`. Those
   series do not exist on a self-hosted cluster, so a direct port is a page of
   "No data" — which on a homepage reads as an outage.

3. COUNTERS vs GAUGES. `temporal_cloud_v1_*` are PRE-COMPUTED PER-SECOND
   GAUGES; you sum() them and never rate() them. Self-hosted server metrics are
   COUNTERS and are meaningless without rate(). Swapping only the metric name
   gives you a number that is wrong rather than absent, which is worse.

4. LABEL NAMES. Three separate conventions collide here, and mixing them
   yields an empty panel with no hint why:
       Cloud            temporal_namespace, temporal_task_queue, temporal_workflow_type
       self-hosted svr  namespace,          taskqueue,           workflowType
       Go/Java SDK      namespace,          task_queue,          workflow_type
   Note `taskqueue` (no underscore) on the server and `task_queue` on the SDK,
   and camelCase `workflowType`/`activityType` on server activity metrics.
   All three verified against a live 1.27.4 cluster.
"""
import json, sys, pathlib

OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent / "demo/grafana/dashboards/custom/temporal-full-overview.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

GOOD, SERIOUS, CRITICAL = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C4, C5 = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
S_LO, S_MID, S_HI = "#9ec5f4", "#5598e7", "#2a78d6"

# Server-side namespace filter. temporal_system is Temporal's OWN internal
# Workers — leaving it in is why `absent(...)` alerts never fire.
NS = 'namespace=~"$namespace", namespace!~"temporal_system|system|_unknown_"'
# Long-polls block up to 60s BY DESIGN and are the highest-volume operations.
# Including them reported 95.9% of requests "under 500ms"; excluding them, 100%.
NLP = 'operation!~"Poll.*|GetTaskQueueUserData|ListNexusEndpoints"'
# Client faults are not your errors. Matching emits ~0.39/s of Canceled at idle;
# counting it took availability to 98.77% while nothing was wrong.
CF = ('error_type!~"serviceerror_(Canceled|NotFound|NamespaceNotFound'
      '|AlreadyExist.*|InvalidArgument|FailedPrecondition'
      '|WorkflowExecutionAlreadyStarted|QueryFailed)"')

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def tgt(expr, legend=None, ref="A"):
    t = {"datasource": DS, "editorMode": "code", "expr": expr,
         "range": True, "instant": False, "refId": ref}
    if legend: t["legendFormat"] = legend
    return t

def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "id": nid(),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}

def stat(title, gp, targets, unit, desc, thr, decimals=None, novalue=None,
         graph="area", color_mode="background"):
    d = {"color": {"mode": "thresholds"}, "unit": unit, "thresholds": thr}
    if decimals is not None: d["decimals"] = decimals
    if novalue: d["noValue"] = novalue
    return {"type": "stat", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": graph,
                        "textMode": "auto", "justifyMode": "auto", "wideLayout": True}}

def ts(title, gp, targets, unit, desc, thr=None, legend_mode="list",
       legend_place="bottom", fill=10, overrides=None, decimals=None,
       minv=None, maxv=None, calcs=None, stack=False):
    d = {"color": {"mode": "thresholds" if thr else "palette-classic"},
         "unit": unit,
         "custom": {"lineWidth": 2, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": False, "gradientMode": "opacity",
                    "lineInterpolation": "smooth", "axisSoftMin": 0}}
    if stack: d["custom"]["stacking"] = {"group": "A", "mode": "normal"}
    if thr: d["thresholds"] = thr
    if decimals is not None: d["decimals"] = decimals
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    return {"type": "timeseries", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": legend_mode, "placement": legend_place,
                                   "showLegend": True, "calcs": calcs or []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def pct(title, gp, metric, extra, desc, unit="s", by=None, legend=None):
    """p50/p95/p99 off one histogram — the source's most repeated shape."""
    grp = f"le, {by}" if by else "le"
    lg = legend or "{{quantile}}"
    return ts(title, gp,
        [tgt(f'histogram_quantile({q}, sum by ({grp}) (rate({metric}{{{extra}}}[$__rate_interval])))',
             legend=(f"p{int(q*100)} " + (lg if by else "")).strip(), ref=r)
         for q, r in ((0.50, "A"), (0.95, "B"), (0.99, "C"))],
        unit, desc,
        overrides=[{"matcher": {"id": "byRegexp", "options": f"p{p}.*"},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                   for p, c in ((50, S_LO), (95, S_MID), (99, S_HI))],
        legend_mode="table", legend_place="bottom", fill=6,
        calcs=["lastNotNull", "max"])

P = []

# =========================================================================
# HEADER — the Cloud board's coloured stat strip, self-hosted.
#
# Dropped from this strip: "Actions/sec" (a Cloud BILLING unit, no self-hosted
# equivalent) and "Queue backlog" (temporal_cloud_v1_approximate_backlog_count
# has no self-hosted gauge). Replaced by request rate and no-poller tasks,
# which answer the same operational question from the server's side.
# =========================================================================
P.append(stat("Running executions", {"h": 4, "w": 4, "x": 0, "y": 0},
    [tgt("sum(temporal_slo_running_executions)")], "short",
    "Open Workflow Executions, from the Visibility monitor in monitor/.\n\nCloud reads temporal_cloud_v1_namespace_open_workflows. Self-hosted has no such gauge — nothing in Prometheus counts executions that have not ended, which is the entire reason monitor/ exists.\n\n'monitor down?' means the monitor is not running, NOT that there is no work.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=0, novalue="monitor down?"))

P.append(stat("Completions / sec", {"h": 4, "w": 4, "x": 4, "y": 0},
    [tgt(f'sum(rate(workflow_success{{{NS}}}[$__rate_interval]))')], "reqps",
    "Successful Workflow completions per second. Server counter + rate(), NOT a Cloud gauge.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None}]}, decimals=2))

P.append(stat("Frontend req / sec", {"h": 4, "w": 4, "x": 8, "y": 0},
    [tgt('sum(rate(service_requests{service_name="frontend"}[$__rate_interval]))')], "reqps",
    "All Frontend RPCs per second, long-polls included.\n\nClosest analogue to the Cloud board's Actions/sec — but NOT the same thing. Actions are a billing unit; this is request volume.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]}, decimals=1))

P.append(stat("Tasks with no poller", {"h": 4, "w": 4, "x": 12, "y": 0},
    [tgt(f'sum(rate(no_poller_tasks{{{NS}}}[$__rate_interval]))')], "reqps",
    "Tasks queued to a Task Queue NOBODY is polling.\n\nThe self-hosted stand-in for the Cloud backlog gauge. Anything above zero is work that waits until a Worker appears — and every other panel stays green while it does.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.01},
                                       {"color": CRITICAL, "value": 1}]}, decimals=2))

P.append(stat("Sync match rate", {"h": 4, "w": 4, "x": 16, "y": 0},
    [tgt(f'''sum(rate(poll_success_sync{{{NS}}}[$__rate_interval]))
 / clamp_min(sum(rate(poll_success{{{NS}}}[$__rate_interval])), 0.001)''')], "percentunit",
    "Tasks handed straight to a waiting Worker vs persisted first. High is good.\n\nReplaces the Cloud board's backlog stat: a falling sync match rate is the earliest sign the fleet is behind, and it LEADS schedule-to-start latency.",
    thr={"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                       {"color": SERIOUS, "value": 0.5},
                                       {"color": GOOD, "value": 0.9}]}, decimals=2))

P.append(stat("Pending requests", {"h": 4, "w": 4, "x": 20, "y": 0},
    [tgt('sum(service_pending_requests{service_name="frontend"})')], "short",
    "Frontend requests in flight. A GAUGE, so no rate().\n\nThe one Cloud stat that ports almost unchanged — service_pending_requests exists on both.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 20},
                                       {"color": CRITICAL, "value": 50}]}, decimals=0))

# =========================================================================
# WORKFLOWS  (Cloud row: "Workflows")
# =========================================================================
P.append(row("Workflows", 4))

P.append(ts("Completions by outcome", {"h": 8, "w": 12, "x": 0, "y": 5},
    [tgt(f'sum(rate(workflow_success{{{NS}}}[$__rate_interval]))', legend="success"),
     tgt(f'sum(rate(workflow_failed{{{NS}}}[$__rate_interval]))', legend="failed", ref="B"),
     tgt(f'sum(rate(workflow_timeout{{{NS}}}[$__rate_interval]))', legend="timeout", ref="C"),
     tgt(f'sum(rate(workflow_terminate{{{NS}}}[$__rate_interval]))', legend="terminated", ref="D")],
    "reqps",
    "The Cloud board splits these across five panels (success/failed/timeout/cancel/continued-as-new). One panel, because the comparison IS the signal.\n\nWATCH TIMEOUT. Saturation on Temporal produces TIMEOUTS, not failures: under a backlog storm workflow_failed sat at 0.02/s while workflow_timeout hit 24.6/s. An SLI watching only failures reported ~100% healthy while three quarters of all work expired.\n\nEXPECT ONLY 'success' ON A HEALTHY STACK — these counters do not exist in Prometheus until they first increment. `make chaos-failures` and `make chaos-backlog` create them.\n\nNOT INCLUDED: workflow_cancel and workflow_continued_as_new, which the Cloud board plots. Neither was observable on this 1.27.4 deployment, and a panel querying a metric that may never exist is worse than an honest omission.",
    overrides=[{"matcher": {"id": "byName", "options": n},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
               for n, c in (("success", C3), ("failed", CRITICAL),
                            ("timeout", C2), ("terminated", C4))],
    legend_mode="table", legend_place="bottom", fill=14, calcs=["lastNotNull", "max"]))

P.append(ts("Completions by Task Queue", {"h": 8, "w": 12, "x": 12, "y": 5},
    [tgt(f'sum by (taskqueue) (rate(workflow_success{{{NS}}}[$__rate_interval]))',
         legend="{{taskqueue}}")],
    "reqps",
    "Cloud groups this by temporal_task_queue. The self-hosted server label is `taskqueue` — ONE WORD, NO UNDERSCORE — while the Go/Java SDK uses `task_queue`. Mixing them gives an empty panel with no error.\n\nThe Cloud original also offers by-workflow-type. Self-hosted server completion counters carry no workflow-type label, so that breakdown lives in the SDK metrics below.",
    legend_mode="table", legend_place="right", fill=12, calcs=["lastNotNull"]))

P.append(pct("Workflow end-to-end latency", {"h": 8, "w": 12, "x": 0, "y": 13},
    "temporal_workflow_endtoend_latency_seconds_bucket", 'namespace=~"$namespace"',
    "Workflow latency percentiles, inclusive of retries and backoff — the Cloud board's three separate p50/p95/p99 panels collapsed into one.\n\nSDK metric, so this is what YOUR WORKERS observed. Seconds on Go and Java; TypeScript, Python and .NET emit milliseconds and this panel is then wrong by 1000x. Run `make verify-sdk-labels`."))

P.append(pct("Workflow task execution latency", {"h": 8, "w": 12, "x": 12, "y": 13},
    "temporal_workflow_task_execution_latency_seconds_bucket", 'namespace=~"$namespace"',
    "How long a single Workflow Task took to execute.\n\nNot on the Cloud board, and it belongs here: this is where sticky-cache evictions surface. A forced eviction means the next Workflow Task REPLAYS the whole history instead of resuming, which shows up as latency here and as nothing at all in the completion counters."))

# =========================================================================
# ACTIVITIES  (Cloud row: "Activities")
# =========================================================================
P.append(row("Activities", 21))

P.append(pct("Activity execution latency (SDK)", {"h": 8, "w": 12, "x": 0, "y": 22},
    "temporal_activity_execution_latency_seconds_bucket", 'namespace=~"$namespace"',
    "Time spent RUNNING an Activity, excluding queue wait. SDK-side.\n\nThis is the panel people expect to move during a backlog and it does not — queue wait is schedule-to-start, in the Pollers row. An Activity can execute in 3ms while the fleet is minutes behind."))

P.append(pct("Activity end-to-end latency (server)", {"h": 8, "w": 12, "x": 12, "y": 22},
    "activity_end_to_end_latency_bucket", 'namespace=~"$namespace"',
    "Schedule-to-close as the SERVER sees it: queue wait plus execution plus retries and backoff. The Cloud board's activity_schedule_to_close_latency.\n\nThe gap between this and the SDK panel to the left IS the queue wait. When they diverge, add Workers.\n\nLABEL TRAP: this server metric carries camelCase `activityType`/`workflowType`, while the SDK metric beside it uses snake_case `activity_type`/`workflow_type`. Same concept, different spelling, silently empty if you mix them."))

P.append(ts("Activity failures and retries", {"h": 8, "w": 12, "x": 0, "y": 30},
    [tgt('sum by (activity_type) (rate(temporal_activity_execution_failed_total{namespace=~"$namespace"}[$__rate_interval]))',
         legend="{{activity_type}}")],
    "reqps",
    "Activity executions that failed, per second, by type.\n\nThe Cloud board separates 'Failures' from 'Failures including retries' using two distinct metrics. The Go SDK exposes only temporal_activity_execution_failed_total, which counts EVERY failed attempt — so this is the 'including retries' number. There is no self-hosted equivalent of the retries-excluded panel.\n\nA retry storm looks like this metric rising while workflow completions stay flat.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.01}]},
    legend_mode="table", legend_place="right", fill=16))

P.append(ts("Activity poll efficiency", {"h": 8, "w": 12, "x": 12, "y": 30},
    [tgt('sum(rate(temporal_activity_poll_no_task_total{namespace=~"$namespace"}[$__rate_interval]))',
         legend="polls returning nothing"),
     tgt('sum(temporal_num_pollers{namespace=~"$namespace"})', legend="pollers", ref="B")],
    "short",
    "Polls that timed out with no Activity Task, and the number of pollers running.\n\nHigh no-task rate with steady pollers is a fleet that is OVER-provisioned for current load — the opposite problem to a backlog, and the one nobody looks for. Near-zero no-task rate means pollers never idle, which means work is queuing.",
    legend_mode="list", legend_place="bottom", fill=8))

# =========================================================================
# POLLERS AND TASK QUEUES  (Cloud row: "Pollers")
# =========================================================================
P.append(row("Pollers and Task Queues", 38))

P.append(ts("Poll outcomes", {"h": 8, "w": 12, "x": 0, "y": 39},
    [tgt(f'sum(rate(poll_success{{{NS}}}[$__rate_interval]))', legend="success"),
     tgt(f'sum(rate(poll_success_sync{{{NS}}}[$__rate_interval]))', legend="sync match", ref="B"),
     tgt(f'''sum(rate(poll_success{{{NS}}}[$__rate_interval]))
 - sum(rate(poll_success_sync{{{NS}}}[$__rate_interval]))''', legend="async match", ref="C"),
     tgt(f'sum(rate(poll_timeouts{{{NS}}}[$__rate_interval]))', legend="timeout", ref="D")],
    "reqps",
    "The Cloud board's four poller stat tiles as one comparison.\n\nMETRIC NAME TRAP: the self-hosted counter is `poll_timeouts` — PLURAL. `poll_timeout` does not exist and returns nothing.\n\nA poll timeout is NORMAL: a poller waited its full long-poll window and no work arrived. Timeouts dominating means an idle cluster, not a broken one.",
    overrides=[{"matcher": {"id": "byName", "options": n},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
               for n, c in (("success", C3), ("sync match", C1),
                            ("async match", C4), ("timeout", C5))],
    legend_mode="table", legend_place="bottom", fill=10, calcs=["lastNotNull", "max"]))

P.append(ts("Sync match rate by Task Queue", {"h": 8, "w": 12, "x": 12, "y": 39},
    [tgt(f'''sum by (taskqueue) (rate(poll_success_sync{{{NS}}}[$__rate_interval]))
 / clamp_min(sum by (taskqueue) (rate(poll_success{{{NS}}}[$__rate_interval])), 0.001)''',
         legend="{{taskqueue}}")],
    "percentunit",
    "Per-queue sync match. This is where a single starved Task Queue shows up while the cluster-wide number still looks fine.",
    thr={"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                       {"color": SERIOUS, "value": 0.5},
                                       {"color": GOOD, "value": 0.9}]},
    decimals=2, minv=0, maxv=1, legend_mode="table", legend_place="right", fill=14))

P.append(ts("Schedule-to-start p99", {"h": 8, "w": 12, "x": 0, "y": 47},
    [tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{namespace=~"$namespace"}}[$__rate_interval])))', legend="activity"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_workflow_task_schedule_to_start_latency_seconds_bucket{{namespace=~"$namespace"}}[$__rate_interval])))', legend="workflow task", ref="B")],
    "s",
    "How long a Task waits before a Worker picks it up. THE fleet-capacity signal, and the one to autoscale on.\n\nNEVER AUTOSCALE A WORKER ON CPU. A Worker blocked in a long poll uses almost no CPU, so when the queue backs up CPU stays flat or FALLS — a CPU-based autoscaler then scales down the fleet that is already behind. It is not a weak signal, it is an inverted one.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.2},
                                       {"color": CRITICAL, "value": 1}]},
    legend_mode="list", legend_place="bottom", fill=12))

P.append(ts("Worker slots and sticky cache", {"h": 8, "w": 12, "x": 12, "y": 47},
    [tgt(f'''sum by (task_queue) (temporal_worker_task_slots_used{{namespace=~"$namespace"}})
/ clamp_min(
    sum by (task_queue) (temporal_worker_task_slots_used{{namespace=~"$namespace"}})
  + sum by (task_queue) (temporal_worker_task_slots_available{{namespace=~"$namespace"}}), 1)''',
         legend="slots used {{task_queue}}"),
     tgt('sum(rate(temporal_sticky_cache_total_forced_eviction_total{namespace=~"$namespace"}[$__rate_interval]))',
         legend="forced evictions/s", ref="B")],
    "percentunit",
    "Worker slot utilisation per Task Queue (SDK label `task_queue`, underscore — unlike the server's `taskqueue`), plus forced sticky-cache evictions.\n\nAt 100% slots with LOW host CPU: raise MaxConcurrentActivityExecutionSize, do not add Workers. At 100% with HIGH host CPU: add Workers. The discriminator is host CPU, which is NOT a Temporal metric — you need node_exporter or cAdvisor or the two cases are indistinguishable.\n\nEvictions are plotted here because they are the hidden cost of a cache that is too small: every eviction forces a full history replay on the next Workflow Task.",
    decimals=2, legend_mode="table", legend_place="right", fill=8))

# =========================================================================
# SERVICE  (Cloud rows: "Service" + "Service Operations")
#
# Replication lag is dropped — Cloud multi-region HA only.
# =========================================================================
P.append(row("Service", 55))

P.append(ts("Requests by operation", {"h": 8, "w": 12, "x": 0, "y": 56},
    [tgt(f'topk(8, sum by (operation) (rate(service_requests{{service_name="frontend", {NS}}}[$__rate_interval])))',
         legend="{{operation}}")],
    "reqps",
    "Top 8 Frontend operations.\n\nThe Cloud board pairs this with a request LIMIT line (temporal_cloud_v1_service_request_limit). Self-hosted has no per-namespace rate limit to plot — you are the capacity. The equivalent question is answered by the Persistence row below.\n\nPoll* dominating is correct. Its ABSENCE is the alarm.",
    legend_mode="table", legend_place="right", fill=12, calcs=["lastNotNull", "max"]))

P.append(ts("Errors by type — server fault only", {"h": 8, "w": 12, "x": 12, "y": 56},
    [tgt(f'sum by (error_type) (rate(service_error_with_type{{{CF}}}[$__rate_interval]))',
         legend="{{error_type}}")],
    "reqps",
    "Frontend errors by type, CLIENT faults filtered out.\n\nThe filter is measured, not stylistic: Matching emits a steady ~0.39/s of serviceerror_Canceled at idle. Counting it took availability to 98.77% and blew a 99.9% budget many times over while nothing was wrong.\n\nEmpty is the healthy state.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.05}]},
    legend_mode="table", legend_place="right", fill=16))

P.append(pct("Frontend latency (long-polls excluded)", {"h": 8, "w": 12, "x": 0, "y": 64},
    "service_latency_bucket", f'service_name="frontend", {NLP}',
    "Frontend latency percentiles with long-polls excluded.\n\nPollWorkflowTaskQueue and PollActivityTaskQueue block up to 60s by design and are the highest-volume operations. Included: 95.9% of requests 'under 500ms'. Excluded: 100%. The unfiltered version fires a latency alert forever on a healthy idle cluster."))

P.append(ts("Resource exhausted", {"h": 8, "w": 12, "x": 12, "y": 64},
    [tgt('sum by (resource_exhausted_cause) (rate(service_errors_resource_exhausted[$__rate_interval]))',
         legend="{{resource_exhausted_cause}}")],
    "reqps",
    "Requests rejected because a limit was hit, broken down by CAUSE.\n\nThe Cloud board plots this too, and it is the one 'quota' panel that survives translation — self-hosted has no per-namespace billing quota, but it does have rate limiters, and resource_exhausted_cause tells you which one fired (RpsLimit, ConcurrentLimit, SystemOverloaded, PersistenceLimit).\n\nThis is the panel to check before concluding the datastore is slow.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": CRITICAL, "value": 0.01}]},
    legend_mode="table", legend_place="right", fill=18))

P.append(pct("StartWorkflowExecution latency", {"h": 7, "w": 8, "x": 0, "y": 72},
    "service_latency_bucket", 'service_name="frontend", operation="StartWorkflowExecution"',
    "The Cloud board's Service Operations row, kept in full — these three operations are the ones a caller actually feels.\n\nStartWorkflowExecution latency is what your API sees when it kicks off work."))

P.append(pct("SignalWorkflowExecution latency", {"h": 7, "w": 8, "x": 8, "y": 72},
    "service_latency_bucket", 'service_name="frontend", operation="SignalWorkflowExecution"',
    "Signal delivery latency. Empty until something signals — this stack's demo app does not, so an empty panel here is correct rather than broken."))

P.append(pct("SignalWithStartWorkflowExecution latency", {"h": 7, "w": 8, "x": 16, "y": 72},
    "service_latency_bucket", 'service_name="frontend", operation="SignalWithStartWorkflowExecution"',
    "Signal-with-start latency. Also empty unless your application uses the pattern."))

# =========================================================================
# PERSISTENCE AND SHARDS
#
# THIS ROW REPLACES three Cloud-only rows: "Usage & Quotas", "Billable
# Actions" and "Provisioned Capacity (TRU)".
#
# All three answer "am I within what I have paid for" — a question a
# self-hosted cluster does not have. The self-hosted version of the same
# worry is "is my infrastructure keeping up", and that is almost always the
# datastore. Most self-hosted Temporal incidents are persistence incidents
# wearing a different hat.
# =========================================================================
P.append(row("Persistence and shards  ·  replaces the Cloud usage, billing and TRU rows", 79))

P.append(pct("Persistence latency", {"h": 8, "w": 12, "x": 0, "y": 80},
    "persistence_latency_bucket", 'operation!=""',
    "Datastore latency percentiles.\n\nCHECK THIS BEFORE SCALING ANY TEMPORAL SERVICE. Persistence is upstream of nearly every Temporal latency symptom, and adding Frontend or History replicas against a saturated datastore makes it worse."))

P.append(ts("Persistence latency p95 by operation", {"h": 8, "w": 12, "x": 12, "y": 80},
    [tgt('topk(6, histogram_quantile(0.95, sum by (operation, le) (rate(persistence_latency_bucket[$__rate_interval]))))',
         legend="{{operation}}")],
    "s",
    "Which datastore operation is slow. CreateWorkflowExecution and UpdateWorkflowExecution dominating is normal; GetTasks or RangeCompleteHistoryTasks climbing usually means queue processing is falling behind.",
    legend_mode="table", legend_place="right", fill=10))

P.append(ts("Persistence errors and request rate", {"h": 8, "w": 12, "x": 0, "y": 88},
    # `or vector(0)` on the error series. persistence_errors has NO SERIES AT
    # ALL until the datastore first fails, so without it this renders "No data"
    # on a perfectly healthy cluster — and a datastore error panel that looks
    # identical whether it is healthy or broken is worse than no panel.
    [tgt('sum(rate(persistence_requests[$__rate_interval]))', legend="requests/s"),
     tgt('sum(rate(persistence_errors[$__rate_interval])) or vector(0)', legend="errors/s", ref="B")],
    "reqps",
    "Datastore throughput against datastore failures.\n\nAny sustained error rate here is the root cause of whatever else you are looking at. Errors with a flat request rate points at the datastore; errors that track request rate points at capacity.",
    overrides=[{"matcher": {"id": "byName", "options": "errors/s"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": CRITICAL}}]},
               {"matcher": {"id": "byName", "options": "requests/s"},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": C1}}]}],
    legend_mode="table", legend_place="bottom", fill=10, calcs=["lastNotNull", "max"]))

P.append(ts("Shard queue lag p95", {"h": 8, "w": 12, "x": 12, "y": 88},
    [tgt('histogram_quantile(0.95, sum by (le, task_category) (rate(shardinfo_immediate_queue_lag_bucket[$__rate_interval])))',
         legend="immediate {{task_category}}"),
     tgt('histogram_quantile(0.95, sum by (le, task_category) (rate(shardinfo_scheduled_queue_lag_bucket[$__rate_interval])))',
         legend="scheduled {{task_category}}", ref="B")],
    "short",
    "How far behind the History service's internal task queues are.\n\nNo Cloud equivalent — Temporal operates the shards for you there. Self-hosted, this is the earliest warning that the History service cannot keep up, and it moves well before user-visible latency does.\n\nSustained growth means timers and transfer tasks are being processed late, which surfaces to users as Workflows that start late for no visible reason.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 100},
                                       {"color": CRITICAL, "value": 1000}]},
    legend_mode="table", legend_place="right", fill=10))

dash = {
    "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
                  "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
    "annotations": {"list": []},
    "description": "Self-hosted rebuild of Grafana Cloud's Temporal overview. Cloud-only rows (billable actions, TRU capacity, action limits, replication lag, schedules) removed; see docs/CLOUD-TO-SELFHOSTED.md for the full mapping.",
    "editable": True, "graphTooltip": 1,
    "links": [{"asDropdown": False, "icon": "external link", "includeVars": True,
               "keepTime": True, "tags": [], "targetBlank": False,
               "title": t, "tooltip": s, "type": "link", "url": u}
              for t, s, u in (
                  ("Overview", "The shallow home board", "/d/temporal-overview"),
                  ("Golden Signals", "RED + Saturation, with trace jump-offs", "/d/temporal-golden-signals"),
                  ("SLO Board", "Error budgets and burn rate", "/d/temporal-slo-board"))],
    "panels": P, "preload": False,
    "refresh": "30s", "schemaVersion": 39,
    "tags": ["temporal", "overview", "self-hosted"],
    "templating": {"list": [
        {"current": {}, "hide": 0, "includeAll": False, "label": "Data source",
         "multi": False, "name": "DS_PROMETHEUS", "options": [], "query": "prometheus",
         "refresh": 1, "regex": "", "skipUrlSync": False, "type": "datasource"},
        # Cloud filters on `temporal_namespace`. Self-hosted uses `namespace`.
        # A ported query keeping temporal_namespace matches nothing, silently.
        {"current": {"selected": True, "text": ["All"], "value": ["$__all"]},
         "datasource": DS, "definition": "label_values(service_requests, namespace)",
         "hide": 0, "includeAll": True, "allValue": ".*", "label": "Namespace",
         "multi": True, "name": "namespace", "options": [],
         "query": {"qryType": 1, "query": "label_values(service_requests, namespace)",
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
    ]},
    "time": {"from": "now-30m", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Temporal — Full Overview (self-hosted)",
    "uid": ("temporal-full-overview" if "demo/" in OUT else "temporal-full-overview-prod"),
    "version": 1, "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
