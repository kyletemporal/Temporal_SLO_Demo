#!/usr/bin/env python3
"""Temporal Overview — the home dashboard.

Modelled on Grafana Cloud's "Temporal overview" (the Cloud Connections
integration), rebuilt for a SELF-HOSTED cluster.

TWO THINGS HAD TO CHANGE, and neither is cosmetic:

1. SCHEMA. The source is `dashboard.grafana.app/v2` — the Grafana 12 dashboard
   schema, with `elements`/`RowsLayout`/`VizConfig`. Grafana 11.5.1, which this
   stack pins, cannot load it at all: it does not parse, it does not degrade.
   This emits classic schemaVersion 39.

2. METRICS. Every query in the source reads `temporal_cloud_v1_*`, which exists
   ONLY on Temporal Cloud's metrics endpoint. On a self-hosted cluster those
   series are absent, so a direct port renders a full page of "No data" —
   which is the worst possible homepage, because it looks like an outage.

   Cloud metrics are also pre-computed per-second GAUGES. `rate()` on them is
   meaningless. Self-hosted server metrics are COUNTERS and require `rate()`.
   Copying a Cloud query and swapping the metric name silently gives you a
   number that is wrong rather than empty.

WHAT HAS NO SELF-HOSTED EQUIVALENT AT ALL, and is therefore absent rather than
empty:

  - Billable actions, TRU / provisioned capacity, action limits. These are
    Cloud BILLING constructs. A self-hosted cluster has no actions and no
    quota; the equivalent question is "is my hardware keeping up", which the
    Saturation row on the golden-signals board answers.
  - Replication lag. Cloud multi-region HA only.
  - approximate_backlog_count. No self-hosted equivalent gauge. The honest
    substitute is sync match rate plus no-poller tasks, both of which measure
    the same thing (is the queue keeping up) from the server's side.
"""
import json, sys, pathlib

OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent / "demo/grafana/dashboards/custom/temporal-overview.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

GOOD, SERIOUS, CRITICAL = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C4, C5 = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
TEMPORAL_INDIGO = "#444CE7"

# Same exclusions as every other board here, for the same measured reasons:
# temporal_system is Temporal's own internal Workers (absence alerts never fire
# without this), and long-polls are 60s BY DESIGN so they poison latency.
TEN = 'namespace=~"$namespace", namespace!~"temporal_system|system|_unknown_"'
NLP = 'operation!~"Poll.*|GetTaskQueueUserData|ListNexusEndpoints"'
CF = ('error_type!~"serviceerror_(Canceled|NotFound|NamespaceNotFound'
      '|AlreadyExist.*|InvalidArgument|FailedPrecondition'
      '|WorkflowExecutionAlreadyStarted|QueryFailed)"')

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]

def tgt(expr, legend=None, ref="A", instant=False):
    t = {"datasource": DS, "editorMode": "code", "expr": expr,
         "range": not instant, "instant": instant, "refId": ref}
    if legend: t["legendFormat"] = legend
    return t

def row(title, y, collapsed=False, panels=None):
    return {"type": "row", "title": title, "collapsed": collapsed, "id": nid(),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": panels or []}

def stat(title, gp, targets, unit, desc, thr, decimals=None, novalue=None,
         graph="area", color_mode="background", text="auto"):
    d = {"color": {"mode": "thresholds"}, "unit": unit, "thresholds": thr}
    if decimals is not None: d["decimals"] = decimals
    if novalue: d["noValue"] = novalue
    return {"type": "stat", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": color_mode, "graphMode": graph,
                        "textMode": text, "justifyMode": "auto", "wideLayout": True}}

def ts(title, gp, targets, unit, desc, thr=None, legend_mode="list",
       legend_place="bottom", fill=10, overrides=None, decimals=None,
       minv=None, maxv=None, calcs=None, style="line", stack=False):
    d = {"color": {"mode": "thresholds" if thr else "palette-classic"},
         "unit": unit,
         "custom": {"lineWidth": 2, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": False, "gradientMode": "opacity",
                    "lineInterpolation": "smooth", "axisSoftMin": 0,
                    "drawStyle": style}}
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

P = []

# =========================================================================
# HERO STRIP — what a platform owner needs before they have a question.
#
# The source dashboard leads with a logo tile and five coloured stats. Kept,
# because it works: a homepage should answer "is anything wrong" from across
# the room, and coloured background stats do that better than a line chart.
#
# The logo is INLINE markup, not the Cloud integration's
# storage.googleapis.com SVG — an air-gapped or offline stack would render a
# broken image as the first thing on the screen.
# =========================================================================
P.append({
    "type": "text", "title": "", "id": nid(), "transparent": True,
    "gridPos": {"h": 5, "w": 3, "x": 0, "y": 0},
    "options": {"mode": "html", "code": {"language": "plaintext",
                                         "showLineNumbers": False, "showMiniMap": False},
                "content": f'''<div style="background:{TEMPORAL_INDIGO};border-radius:3px;
  height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:0.6em;text-align:center;color:#fff;">
  <div style="font-size:15px;font-weight:700;letter-spacing:.02em;">Temporal</div>
  <div style="font-size:11px;opacity:.85;margin-top:2px;">self-hosted</div>
</div>'''}})

P.append(stat("Running executions", {"h": 5, "w": 4, "x": 3, "y": 0},
    [tgt("sum(temporal_slo_running_executions)")],
    "short",
    "Open Workflow Executions, from the Visibility monitor.\n\nThe Cloud original reads temporal_cloud_v1_namespace_open_workflows. Self-hosted has no such gauge — the count comes from monitor/, which polls Visibility. If this reads 'monitor down?', the monitor is not running, NOT that there is no work.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=0, novalue="monitor down?"))

P.append(stat("Completions / sec", {"h": 5, "w": 4, "x": 7, "y": 0},
    [tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval]))')],
    "reqps",
    "Successful Workflow completions per second.\n\nCounter with rate(), not a Cloud gauge — see the header of tools/generate_overview.py for why that distinction silently corrupts a ported query.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None}]},
    decimals=2))

P.append(stat("Frontend requests / sec", {"h": 5, "w": 4, "x": 11, "y": 0},
    [tgt('sum(rate(service_requests{service_name="frontend"}[$__rate_interval]))')],
    "reqps",
    "All Frontend RPCs per second, long-polls included.\n\nThis is the closest self-hosted analogue to the Cloud board's 'Actions/sec'. It is NOT the same thing: Actions are a Cloud BILLING unit and have no self-hosted equivalent. This is request volume, not cost.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=1))

P.append(stat("Tasks with no poller", {"h": 5, "w": 4, "x": 15, "y": 0},
    [tgt(f'sum(rate(no_poller_tasks{{{TEN}}}[$__rate_interval]))')],
    "reqps",
    "Tasks added to a Task Queue that NOBODY is polling.\n\nThe self-hosted stand-in for the Cloud board's task backlog gauge (temporal_cloud_v1_approximate_backlog_count), which does not exist here.\n\nAnything above zero is work that will sit until a Worker appears. This is the single most under-watched number on a self-hosted cluster — it is how an orphaned Task Queue looks, and every other panel stays green.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.01},
                                       {"color": CRITICAL, "value": 1}]},
    decimals=2))

P.append(stat("Namespaces with traffic", {"h": 5, "w": 5, "x": 19, "y": 0},
    [tgt(f'count(count by (namespace) (rate(service_requests{{service_name="frontend", {TEN}}}[$__rate_interval]) > 0))')],
    "short",
    "Namespaces that served at least one Frontend request in the window.\n\nA Namespace that exists but appears here as absent is either idle or has no Workers — and those look identical from metrics alone. Cross-check with the Tasks-with-no-poller panel.",
    thr={"mode": "absolute", "steps": [{"color": C5, "value": None}]},
    decimals=0, graph="none"))

# =========================================================================
# HEALTH — the three questions in order of how often they are the answer.
# =========================================================================
P.append(row("Health", 5))

P.append(ts("Frontend request rate by operation", {"h": 8, "w": 12, "x": 0, "y": 6},
    [tgt(f'topk(6, sum by (operation) (rate(service_requests{{service_name="frontend", {TEN}}}[$__rate_interval])))',
         legend="{{operation}}")],
    "reqps",
    "Top 6 Frontend operations by rate.\n\nPoll* operations dominate on any healthy cluster and that is correct — Workers long-poll. Their ABSENCE is the signal worth reacting to.",
    legend_mode="table", legend_place="right", fill=14, calcs=["lastNotNull", "max"]))

P.append(ts("Errors — server fault only", {"h": 8, "w": 12, "x": 12, "y": 6},
    [tgt(f'sum by (error_type) (rate(service_error_with_type{{{CF}}}[$__rate_interval]))',
         legend="{{error_type}}")],
    "reqps",
    "Frontend errors by type, with CLIENT faults filtered out.\n\nThe filter is load-bearing and measured: Matching emits a steady ~0.39/s of serviceerror_Canceled at idle. Counting it took availability to 98.77% and blew a 99.9% error budget many times over while nothing was wrong.\n\nAn empty panel here is the correct state.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 0.05}]},
    legend_mode="table", legend_place="right", fill=16))

P.append(ts("Frontend latency (long-polls excluded)", {"h": 8, "w": 12, "x": 0, "y": 14},
    [tgt(f'histogram_quantile(0.50, sum by (le) (rate(service_latency_bucket{{service_name="frontend", {NLP}}}[$__rate_interval])))', legend="p50"),
     tgt(f'histogram_quantile(0.95, sum by (le) (rate(service_latency_bucket{{service_name="frontend", {NLP}}}[$__rate_interval])))', legend="p95", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(service_latency_bucket{{service_name="frontend", {NLP}}}[$__rate_interval])))', legend="p99", ref="C")],
    "s",
    "Frontend latency percentiles, WITH LONG-POLLS EXCLUDED.\n\nPollWorkflowTaskQueue and PollActivityTaskQueue block up to 60s by design and are the highest-volume operations. Included: 95.9% of requests 'under 500ms'. Excluded: 100%. The unfiltered version of this panel fires a latency alert forever on a healthy idle cluster.",
    legend_mode="list", legend_place="bottom", fill=8))

P.append(ts("Workflow outcomes", {"h": 8, "w": 12, "x": 12, "y": 14},
    [tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval]))', legend="success"),
     tgt(f'sum(rate(workflow_failed{{{TEN}}}[$__rate_interval]))', legend="failed", ref="B"),
     tgt(f'sum(rate(workflow_timeout{{{TEN}}}[$__rate_interval]))', legend="timeout", ref="C")],
    "reqps",
    "Completions by outcome.\n\nWATCH THE TIMEOUT SERIES. Saturation on Temporal produces TIMEOUTS, not failures: under a backlog storm, workflow_failed sat at 0.02/s while workflow_timeout hit 24.6/s. An SLI watching only failures reported ~100% healthy while three quarters of all work expired.\n\nEXPECT ONLY 'success' ON A HEALTHY STACK. SDK counters are not created until they first increment, so workflow_failed and workflow_timeout are absent from Prometheus entirely — not zero, ABSENT — until something actually fails. `make chaos-failures` and `make chaos-backlog` bring them into existence.",
    overrides=[{"matcher": {"id": "byName", "options": n},
                "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
               for n, c in (("success", C3), ("failed", CRITICAL), ("timeout", C2))],
    legend_mode="table", legend_place="bottom", fill=14, calcs=["lastNotNull", "max"]))

# =========================================================================
# TASK QUEUES — where "durable but not progressing" becomes visible.
# =========================================================================
P.append(row("Task queues and Workers", 22))

P.append(ts("Sync match rate", {"h": 7, "w": 12, "x": 0, "y": 23},
    [tgt(f'''sum(rate(poll_success_sync{{{TEN}}}[$__rate_interval]))
  / clamp_min(sum(rate(poll_success{{{TEN}}}[$__rate_interval])), 0.001)''',
         legend="sync match")],
    "percentunit",
    "Fraction of Tasks handed straight to a waiting Worker instead of being persisted first.\n\nThe self-hosted replacement for the Cloud board's backlog gauge. High is good — it means Workers are keeping up. A FALLING sync match rate is the earliest sign the fleet is behind, and it leads schedule-to-start latency.",
    thr={"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                       {"color": SERIOUS, "value": 0.5},
                                       {"color": GOOD, "value": 0.9}]},
    decimals=2, minv=0, maxv=1, fill=16))

P.append(ts("Schedule-to-start p99", {"h": 7, "w": 12, "x": 12, "y": 23},
    [tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', legend="activity"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_workflow_task_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', legend="workflow task", ref="B")],
    "s",
    "How long a Task waits before a Worker picks it up.\n\nTHE fleet-capacity signal, and the one to autoscale on — never CPU. A Worker blocked in a long poll uses almost no CPU, so a CPU-based autoscaler scales DOWN a fleet that is falling behind.\n\nUNITS: Go and Java emit seconds. TypeScript, Python and .NET emit milliseconds, and this panel would then be wrong by 1000x. Run `make verify-sdk-labels`.",
    legend_mode="list", legend_place="bottom", fill=10))

P.append(ts("Worker slot utilisation", {"h": 7, "w": 12, "x": 0, "y": 30},
    [tgt(f'''sum by (task_queue) (temporal_worker_task_slots_used{{{TEN}}})
/ clamp_min(
    sum by (task_queue) (temporal_worker_task_slots_used{{{TEN}}})
  + sum by (task_queue) (temporal_worker_task_slots_available{{{TEN}}}), 1)''',
         legend="{{task_queue}}")],
    "percentunit",
    "Fraction of Worker capacity in use, per Task Queue.\n\nAt 100% with LOW host CPU: raise MaxConcurrentActivityExecutionSize, do not add Workers. At 100% with HIGH host CPU: add Workers.\n\nThat discriminator is host CPU, which is NOT a Temporal metric — you need node_exporter or cAdvisor alongside this, or the two cases are indistinguishable.",
    decimals=2, minv=0, maxv=1, legend_mode="table", legend_place="right", fill=12))

P.append(ts("Persistence p95 by operation", {"h": 7, "w": 12, "x": 12, "y": 30},
    [tgt('topk(5, histogram_quantile(0.95, sum by (operation, le) (rate(persistence_latency_bucket[$__rate_interval]))))',
         legend="{{operation}}")],
    "s",
    "Datastore latency, top 5 operations.\n\nCHECK THIS BEFORE SCALING ANY TEMPORAL SERVICE. The datastore is upstream of nearly every Temporal latency symptom — most self-hosted incidents are persistence incidents wearing a different hat.",
    legend_mode="table", legend_place="right", fill=10))

# =========================================================================
# SLO — the promise, and the executions nothing else can see.
# =========================================================================
P.append(row("Service level", 37))

P.append(stat("SLOs in breach", {"h": 6, "w": 4, "x": 0, "y": 38},
    # `or vector(0)` is load-bearing. count() over a vector that matches
    # NOTHING returns an empty result, not 0 — so on a perfectly healthy stack
    # this panel rendered "No data" where it should read zero. That is the
    # worst possible failure for a breach counter: the all-clear and the
    # broken-query state look identical.
    [tgt("count(slo:error_budget_remaining:ratio <= 0) or vector(0)")],
    "short",
    "SLIs whose 28-day error budget is exhausted. Zero is the expected state.\n\nThe query ends in `or vector(0)` deliberately — count() of an empty match is empty, not zero, so without it a healthy stack shows 'No data' here.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": CRITICAL, "value": 1}]},
    decimals=0, novalue="0", graph="none"))

P.append(ts("Error budget remaining", {"h": 6, "w": 10, "x": 4, "y": 38},
    [tgt("slo:error_budget_remaining:ratio", legend="{{sli}}")],
    "percentunit",
    "Fraction of the 28-day error budget still available, per SLI.\n\nNegative means overspent. Budgets are cumulative over 28 days — `make down` keeps them, `make reset` wipes them.",
    thr={"mode": "absolute", "steps": [{"color": CRITICAL, "value": None},
                                       {"color": SERIOUS, "value": 0.25},
                                       {"color": GOOD, "value": 0.5}]},
    decimals=2, legend_mode="table", legend_place="right", fill=10))

P.append(ts("Open executions past budget", {"h": 6, "w": 10, "x": 14, "y": 38},
    [tgt("sum by (bucket) (temporal_slo_over_budget_executions)", legend="past {{bucket}}x budget")],
    "short",
    "OPEN executions that have exceeded N x their duration budget.\n\nTHE ROW NOTHING ELSE ON THIS PAGE CAN SEE. These executions are Running, pollers are healthy, nothing has failed and nothing is retrying — only duration is wrong, and no Prometheus counter carries duration for an execution that has not ended.\n\nReproduce with `make chaos-stuck`: this moves 0 -> 5 and nothing else on the stack reacts.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": SERIOUS, "value": 1}]},
    legend_mode="table", legend_place="bottom", fill=16))

# =========================================================================
# NAVIGATION — a homepage's real job is routing, not analysis.
# =========================================================================
BOARDS = [
    ("Full Overview", "Every row of the Grafana Cloud board, translated to self-hosted",
     C4, "/d/temporal-full-overview"),
    ("Golden Signals", "RED + Saturation, with the trace and profile jump-offs",
     C1, "/d/temporal-golden-signals"),
    ("SLO Board", "Error budgets and burn rate, per SLI",
     C3, "/d/temporal-slo-board"),
    ("Service & Worker Overview", "The original per-service detail board",
     C5, "/d/temporal-self-hosted-overview"),
    ("Temporal UI", "Workflow histories and execution search",
     C2, "http://localhost:8080"),
]

cards = "".join(f"""
  <a href="{url}" style="flex:1 1 0;min-width:170px;text-decoration:none;color:inherit;
      display:block;padding:11px 13px;border-radius:3px;
      border:1px solid rgba(127,127,127,.28);border-left:3px solid {c};
      background:rgba(127,127,127,.08);">
    <div style="font-weight:600;font-size:13px;">{t} <span style="opacity:.5;">&rarr;</span></div>
    <div style="font-size:11px;line-height:1.35;opacity:.72;margin-top:3px;">{s}</div>
  </a>""" for t, s, c, url in BOARDS)

P.append({"type": "text", "title": "Go deeper", "id": nid(), "transparent": True,
          "gridPos": {"h": 4, "w": 24, "x": 0, "y": 44},
          "description": "This board is deliberately shallow — it answers 'is anything wrong', not 'why'. These are the boards that answer why.",
          "options": {"mode": "html",
                      "code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False},
                      "content": f'<div style="display:flex;gap:8px;flex-wrap:wrap;">{cards}\n</div>'}})

dash = {
    "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
                  "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
    "annotations": {"list": []},
    "description": "Home dashboard for a self-hosted Temporal Service. Modelled on Grafana Cloud's Temporal overview, rebuilt for self-hosted metrics and the classic dashboard schema.",
    "editable": True, "graphTooltip": 1,
    "links": [{"asDropdown": False, "icon": "external link", "includeVars": False,
               "keepTime": True, "tags": [], "targetBlank": False,
               "title": t, "tooltip": s, "type": "link", "url": u}
              for t, s, _c, u in BOARDS],
    "panels": P, "preload": False,
    "refresh": "30s", "schemaVersion": 39,
    "tags": ["temporal", "overview", "home"],
    "templating": {"list": [
        {"current": {}, "hide": 0, "includeAll": False, "label": "Data source",
         "multi": False, "name": "DS_PROMETHEUS", "options": [], "query": "prometheus",
         "refresh": 1, "regex": "", "skipUrlSync": False, "type": "datasource"},
        # The Cloud original filters on temporal_namespace; self-hosted server
        # metrics use `namespace`. Same idea, different label — a ported query
        # that keeps temporal_namespace matches nothing and renders empty.
        {"current": {"selected": True, "text": ["All"], "value": ["$__all"]},
         "datasource": DS, "definition": "label_values(service_requests, namespace)",
         "hide": 0, "includeAll": True, "allValue": ".*", "label": "Namespace",
         "multi": True, "name": "namespace", "options": [],
         "query": {"qryType": 1, "query": "label_values(service_requests, namespace)",
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
    ]},
    "time": {"from": "now-30m", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Temporal — Overview",
    "uid": ("temporal-overview" if "demo/" in OUT else "temporal-overview-prod"),
    "version": 1, "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
