#!/usr/bin/env python3
"""One-screen dashboard for a Temporal application team. Minimum standard."""
import json, pathlib, pathlib

OUT = str(pathlib.Path(__file__).resolve().parent.parent / "app-team/grafana/dashboards/temporal-app-team.json")
DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
GOOD, SERIOUS, CRITICAL = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3 = "#3987e5", "#d95926", "#199e70"
S_LO, S_MID, S_HI = "#9ec5f4", "#5598e7", "#2a78d6"

# Dashboard variables, so a team fills these in once in the UI instead of
# editing every panel. Defaults match the demo stack.
SC = 'namespace="$namespace", task_queue="$task_queue"'

_id = [0]
def nid():
    _id[0] += 1; return _id[0]

def tgt(expr, legend=None, ref="A", instant=False):
    t = {"datasource": DS, "editorMode": "code", "expr": expr,
         "range": not instant, "instant": instant, "refId": ref}
    if legend: t["legendFormat"] = legend
    return t

def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "id": nid(),
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}

def thr(steps): return {"mode": "absolute", "steps": steps}
def ov(n, c): return {"matcher": {"id": "byName", "options": n},
                      "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}

def ts(title, gp, targets, unit, desc, overrides=None, thresholds=None,
       decimals=None, minv=None, maxv=None, legend="list", place="bottom", fill=8):
    d = {"color": {"mode": "thresholds" if thresholds else "palette-classic"}, "unit": unit,
         "custom": {"lineWidth": 2, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": False, "gradientMode": "opacity",
                    "lineInterpolation": "smooth", "axisSoftMin": 0}}
    if thresholds: d["thresholds"] = thresholds
    if decimals is not None: d["decimals"] = decimals
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    return {"type": "timeseries", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": legend, "placement": place, "showLegend": True, "calcs": []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def stat(title, gp, targets, unit, desc, thresholds, decimals=None, graph="area",
         text="auto", cmode="value", novalue=None, minv=None, maxv=None):
    d = {"color": {"mode": "thresholds"}, "unit": unit, "thresholds": thresholds}
    if decimals is not None: d["decimals"] = decimals
    if novalue: d["noValue"] = novalue
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    return {"type": "stat", "title": title, "id": nid(), "datasource": DS, "gridPos": gp,
            "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": cmode, "graphMode": graph, "textMode": text,
                        "justifyMode": "auto", "wideLayout": True}}

P = []

# ---- 1. Am I OK? -----------------------------------------------------------
P.append(row("Am I OK?", 0))

P.append(stat("Workers alive", {"h": 6, "w": 5, "x": 0, "y": 1},
    [tgt(f'count(count by (instance) (temporal_worker_task_slots_available{{{SC}}})) or vector(0)', instant=True)],
    "none",
    "How many Worker processes are reporting.\n\nZERO IS THE ALARM. When your fleet dies these metrics stop EXISTING rather than going to zero, so a threshold alert cannot catch it — that is what AppWorkerFleetAbsent is for. This tile shows 0 via `or vector(0)`.",
    thr([{"color": CRITICAL, "value": None}, {"color": GOOD, "value": 1}]),
    graph="none", text="value", cmode="background"))

P.append(stat("Error budget remaining", {"h": 6, "w": 11, "x": 5, "y": 1},
    [tgt("clamp_min(appslo:error_budget_remaining:ratio, -1)", legend="{{sli}}", instant=True)],
    "percentunit",
    "One tile per SLO. Green >25% left, orange 0-25%, red means the SLO has been missed for the window. Saturates at -100%.\n\nRead the number, not only the colour — green and orange are near-indistinguishable under protanopia.",
    thr([{"color": CRITICAL, "value": None}, {"color": SERIOUS, "value": 0.0001}, {"color": GOOD, "value": 0.25}]),
    decimals=0, graph="none", text="value_and_name", cmode="background",
    novalue="no traffic", minv=-1, maxv=1))

P.append(stat("Workflow failure ratio (5m)", {"h": 6, "w": 8, "x": 16, "y": 1},
    [tgt(f'''(
  sum(rate(temporal_workflow_failed_total{{{SC}}}[$__rate_interval]))
  or sum(rate(temporal_workflow_completed_total{{{SC}}}[$__rate_interval])) * 0
)
/ clamp_min(
  sum(rate(temporal_workflow_completed_total{{{SC}}}[$__rate_interval]))
  + (sum(rate(temporal_workflow_failed_total{{{SC}}}[$__rate_interval]))
     or sum(rate(temporal_workflow_completed_total{{{SC}}}[$__rate_interval])) * 0),
  0.001)''')],
    "percentunit",
    "Terminal Workflow failures — NOT Activity failures, which retries absorb and which you should not page on.\n\nThe `or … * 0` guard matters: temporal_workflow_failed_total does not exist until your first failure, and without the guard this reads empty exactly when everything is failing.",
    thr([{"color": GOOD, "value": None}, {"color": SERIOUS, "value": 0.01}, {"color": CRITICAL, "value": 0.05}]),
    decimals=3, cmode="background"))

# ---- 2. Is work flowing? ---------------------------------------------------
P.append(row("Is work flowing?", 7))

P.append(ts("Workflow outcomes", {"h": 8, "w": 8, "x": 0, "y": 8},
    [tgt(f'sum(rate(temporal_workflow_completed_total{{{SC}}}[$__rate_interval]))', legend="completed"),
     tgt(f'sum(rate(temporal_workflow_failed_total{{{SC}}}[$__rate_interval]))', legend="failed", ref="B")],
    "reqps",
    "'failed' is absent until your first failure — an empty line here is usually 'never happened', not 'healthy'. Prove your alerting by causing a failure, not by waiting for one.",
    overrides=[ov("completed", GOOD), ov("failed", CRITICAL)], fill=18))

P.append(ts("Task delivery — schedule-to-start", {"h": 8, "w": 8, "x": 8, "y": 8},
    [tgt(f'histogram_quantile(0.50, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{SC}}}[$__rate_interval])))', legend="p50"),
     tgt(f'histogram_quantile(0.95, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{SC}}}[$__rate_interval])))', legend="p95", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{SC}}}[$__rate_interval])))', legend="p99", ref="C")],
    "s",
    "QUEUE WAIT, not execution time — how long work sat before a Worker took it. The earliest customer-visible sign of an undersized fleet.\n\nIf this is suspiciously flat during a known backlog, you have set ScheduleToStartTimeout in ActivityOptions; it truncates the metric and hides the problem. Leave it unset.",
    overrides=[ov("p50", S_LO), ov("p95", S_MID), ov("p99", S_HI)],
    thresholds=thr([{"color": GOOD, "value": None}, {"color": SERIOUS, "value": 1}]), fill=0))

P.append(ts("Workflow end-to-end latency", {"h": 8, "w": 8, "x": 16, "y": 8},
    [tgt(f'histogram_quantile(0.50, sum by (le) (rate(temporal_workflow_endtoend_latency_seconds_bucket{{{SC}}}[$__rate_interval])))', legend="p50"),
     tgt(f'histogram_quantile(0.95, sum by (le) (rate(temporal_workflow_endtoend_latency_seconds_bucket{{{SC}}}[$__rate_interval])))', legend="p95", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le) (rate(temporal_workflow_endtoend_latency_seconds_bucket{{{SC}}}[$__rate_interval])))', legend="p99", ref="C")],
    "s",
    "The metric closest to what your users actually feel.\n\nNote the SDK's default buckets stop at 10s: anything slower lands in +Inf and the quantile flattens against that ceiling. If your Workflows routinely exceed 10s, configure custom histogram buckets in your Worker.",
    overrides=[ov("p50", S_LO), ov("p95", S_MID), ov("p99", S_HI)], fill=0))

# ---- 3. Have I got capacity? ----------------------------------------------
P.append(row("Have I got capacity?", 16))

P.append(ts("Worker slot utilisation", {"h": 8, "w": 12, "x": 0, "y": 17},
    [tgt(f'''sum by (worker_type) (temporal_worker_task_slots_used{{{SC}}})
/ clamp_min(
    sum by (worker_type) (temporal_worker_task_slots_used{{{SC}}})
  + sum by (worker_type) (temporal_worker_task_slots_available{{{SC}}}), 1)''', legend="{{worker_type}}")],
    "percentunit",
    "At 100% with LOW host CPU, raise MaxConcurrentActivityExecutionSize — do not add Workers. At 100% with HIGH host CPU, add Workers.\n\nHost CPU is not a Temporal metric. Without it from your platform team these two cases are indistinguishable and the fixes are opposite.",
    minv=0, maxv=1, decimals=2,
    thresholds=thr([{"color": GOOD, "value": None}, {"color": SERIOUS, "value": 0.8}, {"color": CRITICAL, "value": 0.99}]),
    legend="table", place="right"))

P.append(ts("Can my Workers reach Temporal?", {"h": 8, "w": 12, "x": 12, "y": 17},
    [tgt(f'sum(rate(temporal_request_total{{namespace="$namespace"}}[$__rate_interval]))', legend="requests"),
     tgt(f'sum(rate(temporal_request_failure_total{{namespace="$namespace"}}[$__rate_interval]))', legend="failures", ref="B")],
    "reqps",
    "Your Workers' own calls to the Temporal Service. Separates 'my code is broken' from 'I cannot reach the platform' — which is a conversation with your platform team, not a code change.\n\ntemporal_request_failure_total is absent until the first failure, so a missing line is silent, not healthy.",
    overrides=[ov("requests", C1), ov("failures", CRITICAL)], fill=0))

def var(name, query, default):
    return {"current": {"selected": False, "text": default, "value": default},
            "definition": query, "hide": 0, "includeAll": False, "multi": False,
            "name": name, "options": [], "query": {"qryType": 1, "query": query, "refId": name},
            "refresh": 1, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"}

dash = {
    "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
                  "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
    "annotations": {"list": []},
    "description": "Minimum observability standard for a Temporal application team: is it OK, is work flowing, have I got capacity.",
    "editable": True, "graphTooltip": 1, "links": [], "panels": P, "preload": False,
    "refresh": "1m", "schemaVersion": 39,
    "tags": ["temporal", "app-team", "minimum-standard", "slo"],
    "templating": {"list": [
        {"current": {}, "hide": 0, "includeAll": False, "label": "Data source", "multi": False,
         "name": "DS_PROMETHEUS", "options": [], "query": "prometheus", "refresh": 1,
         "regex": "", "skipUrlSync": False, "type": "datasource"},
        var("namespace", "label_values(temporal_worker_task_slots_available, namespace)", "default"),
        var("task_queue", "label_values(temporal_worker_task_slots_available{namespace=\"$namespace\"}, task_queue)", "orders"),
    ]},
    "time": {"from": "now-6h", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Temporal — Application Team (minimum standard)",
    "uid": "temporal-app-team", "version": 1, "weekStart": "",
}

pathlib.Path(OUT).parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "w") as f:
    json.dump(dash, f, indent=2); f.write("\n")
print(f"wrote {OUT}\n  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
