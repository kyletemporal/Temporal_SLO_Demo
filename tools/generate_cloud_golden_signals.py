#!/usr/bin/env python3
"""Temporal Cloud — RED + Saturation golden signals, with SLOs on top."""
import json

import pathlib
OUT = str(pathlib.Path(__file__).resolve().parent.parent / "cloud/grafana/dashboards/temporal-cloud-golden-signals.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
GOOD, SERIOUS, CRITICAL = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C4, C5 = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
S_LO, S_MID, S_HI = "#9ec5f4", "#5598e7", "#2a78d6"

# Cloud `_count` metrics are GAUGES holding pre-computed per-second rates.
# avg_over_time widens the window; rate() would be meaningless.
W = "$__interval"

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

def thr(steps):
    return {"mode": "absolute", "steps": steps}

def ov(name, hexv):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexv}}]}

def ts(title, gp, targets, unit, desc, overrides=None, thresholds=None,
       decimals=None, minv=None, maxv=None, legend="list", place="bottom",
       calcs=None, fill=8):
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
            "options": {"legend": {"displayMode": legend, "placement": place,
                                   "showLegend": True, "calcs": calcs or []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def stat(title, gp, targets, unit, desc, thresholds, decimals=None, graph="area",
         text="auto", cmode="value", novalue=None, minv=None, maxv=None):
    d = {"color": {"mode": "thresholds"}, "unit": unit, "thresholds": thresholds}
    if decimals is not None: d["decimals"] = decimals
    if novalue: d["noValue"] = novalue
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    return {"type": "stat", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": cmode, "graphMode": graph, "textMode": text,
                        "justifyMode": "auto", "wideLayout": True}}

P = []

# ---------------------------------------------------------------- SLO
P.append(row("Service level — yours, on top of Temporal's SLA", 0))

P.append(stat("SLOs in breach", {"h": 8, "w": 5, "x": 0, "y": 1},
    [tgt("count(cloudslo:error_budget_remaining:ratio <= 0) or vector(0)", instant=True)],
    "none",
    "Zero means every error budget still has room.\n\nRemember the split: cloud_service_availability is mostly Temporal's; workflow_completion, activity_completion and task_delivery are yours. Only the first is an SLA conversation.",
    thr([{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 1}]),
    graph="none", text="value", cmode="background"))

P.append(stat("Error budget remaining", {"h": 8, "w": 11, "x": 5, "y": 1},
    [tgt("clamp_min(cloudslo:error_budget_remaining:ratio, -1)",
         legend="{{sli}} {{temporal_namespace}}", instant=True)],
    "percentunit",
    "One tile per SLO per Namespace. Saturates at -100%.\n\nRead the number, not only the colour — green vs orange is near-indistinguishable under protanopia.",
    thr([{"color": CRITICAL, "value": None}, {"color": SERIOUS, "value": 0.0001},
         {"color": GOOD, "value": 0.25}]),
    decimals=0, graph="none", text="value_and_name", cmode="background",
    novalue="no traffic", minv=-1, maxv=1))

P.append(ts("Burn rate (1h)", {"h": 8, "w": 8, "x": 16, "y": 1},
    [tgt("cloudslo:burn_rate:ratio_rate1h", legend="{{sli}} {{temporal_namespace}}")],
    "none",
    "Multiples of sustainable budget spend. 14.4x is the fast-burn page threshold.\n\nIf cloud_service_availability is burning, check status.temporal.io before assuming it is yours.",
    thresholds=thr([{"color": GOOD, "value": None}, {"color": SERIOUS, "value": 1},
                    {"color": CRITICAL, "value": 14.4}]), fill=0))

# ---------------------------------------------------------------- RATE
P.append(row("R — Rate  ·  how much work is arriving", 9))

P.append(stat("Namespace request rate", {"h": 7, "w": 5, "x": 0, "y": 10},
    [tgt(f"sum(avg_over_time(temporal_cloud_v1_service_request_count[{W}]))")],
    "reqps",
    "All gRPC requests to the Namespace. This metric is already a per-second rate — avg_over_time widens the window; rate() would be wrong.",
    thr([{"color": C1, "value": None}]), decimals=1))

P.append(ts("Request rate by operation", {"h": 7, "w": 9, "x": 5, "y": 10},
    [tgt(f"topk(6, sum by (operation) (avg_over_time(temporal_cloud_v1_service_request_count[{W}])))",
         legend="{{operation}}")],
    "reqps",
    "Top 6 operations. Long-poll operations normally dominate — that is Workers waiting for work, not load.",
    legend="table", place="right", calcs=["lastNotNull"]))

P.append(ts("Workflow throughput", {"h": 7, "w": 10, "x": 14, "y": 10},
    [tgt(f'sum(avg_over_time(temporal_cloud_v1_service_request_count{{operation="StartWorkflowExecution"}}[{W}]))', legend="started"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_workflow_success_count[{W}]))", legend="completed", ref="B")],
    "reqps",
    "Started vs completed. A persistent gap means work is accumulating — visible here long before anything errors.",
    overrides=[ov("started", C1), ov("completed", C3)]))

# ---------------------------------------------------------------- ERRORS
P.append(row("E — Errors  ·  and whose fault they are", 17))

P.append(stat("Service error ratio", {"h": 7, "w": 5, "x": 0, "y": 18},
    [tgt(f"""avg_over_time(temporal_cloud_v1_service_error_count[{W}])
/ clamp_min(avg_over_time(temporal_cloud_v1_service_request_count[{W}]), 0.001)""")],
    "percentunit",
    "CONSERVATIVE: service_error_count has no error-type label, so SLA-excluded errors (NotFound, InvalidArgument, PermissionDenied, Throttling) are counted here and cannot be filtered out.\n\nThis reads WORSE than Temporal's own SLA figure. Do not use it to argue a service credit.",
    thr([{"color": GOOD, "value": None}, {"color": SERIOUS, "value": 0.001},
         {"color": CRITICAL, "value": 0.01}]), decimals=3, cmode="background"))

P.append(ts("Errors by operation", {"h": 7, "w": 9, "x": 5, "y": 18},
    [tgt(f"topk(6, sum by (operation) (avg_over_time(temporal_cloud_v1_service_error_count[{W}])))",
         legend="{{operation}}")],
    "reqps",
    "Which call is failing. A single operation dominating usually points at your code; broad elevation across operations points at Temporal — check status.temporal.io.",
    legend="table", place="right", calcs=["lastNotNull"]))

P.append(ts("Workflow outcomes", {"h": 7, "w": 10, "x": 14, "y": 18},
    [tgt(f"sum(avg_over_time(temporal_cloud_v1_workflow_success_count[{W}]))", legend="success"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_workflow_failed_count[{W}]))", legend="failed", ref="B"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_workflow_timeout_count[{W}]))", legend="timeout", ref="C")],
    "reqps",
    "Your application, not Temporal. Watch timeout specifically — a saturated Worker fleet produces timeouts rather than failures, and an alert watching only failures stays silent through it.",
    overrides=[ov("success", GOOD), ov("failed", CRITICAL), ov("timeout", SERIOUS)],
    fill=18))

# ---------------------------------------------------------------- DURATION
P.append(row("D — Duration  ·  pre-calculated percentiles, do not aggregate", 25))

P.append(ts("StartWorkflowExecution latency", {"h": 7, "w": 8, "x": 0, "y": 26},
    [tgt('temporal_cloud_v1_service_latency_p50{operation="StartWorkflowExecution"}', legend="p50"),
     tgt('temporal_cloud_v1_service_latency_p95{operation="StartWorkflowExecution"}', legend="p95", ref="B"),
     tgt('temporal_cloud_v1_service_latency_p99{operation="StartWorkflowExecution"}', legend="p99", ref="C")],
    "s",
    "Pre-calculated per 1-minute window. NOT aggregated across operations — Temporal's docs are explicit that aggregating a percentile produces a wrong number.\n\nOn a low-traffic Namespace p50/p95/p99 converge on the single slowest request. Tail percentiles need ~20+ samples per minute to mean anything.",
    overrides=[ov("p50", S_LO), ov("p95", S_MID), ov("p99", S_HI)], fill=0))

P.append(ts("Workflow schedule-to-close p95", {"h": 7, "w": 8, "x": 8, "y": 26},
    [tgt("temporal_cloud_v1_workflow_schedule_to_close_latency_p95", legend="{{temporal_workflow_type}}")],
    "s",
    "End-to-end Workflow duration by type. The closest thing to what your customer experiences.\n\nRising here with flat service latency means your Workers or your dependencies, not Temporal.",
    legend="table", place="right", calcs=["lastNotNull"], fill=0))

P.append(ts("Activity start-to-close p95", {"h": 7, "w": 8, "x": 16, "y": 26},
    [tgt("temporal_cloud_v1_activity_start_to_close_latency_p95", legend="{{temporal_activity_type}}")],
    "s",
    "How long Activities take once started. Labelled only by activity type — task queue and workflow type are deliberately excluded upstream because pre-calculated percentiles cannot be split further.\n\nRequires the opt-in temporal_activity_type label on your scrape URL.",
    legend="table", place="right", calcs=["lastNotNull"], fill=0))

# ---------------------------------------------------------------- SATURATION
P.append(row("S — Saturation  ·  on Cloud this means LIMITS", 33))

P.append(ts("Usage against limits", {"h": 8, "w": 8, "x": 0, "y": 34},
    [tgt("temporal_cloud_v1_total_action_count / clamp_min(temporal_cloud_v1_action_limit, 1)", legend="actions"),
     tgt("temporal_cloud_v1_service_request_count / clamp_min(temporal_cloud_v1_service_request_limit, 1)", legend="requests", ref="B"),
     tgt("temporal_cloud_v1_service_pending_requests / clamp_min(temporal_cloud_v1_poller_limit, 1)", legend="pollers", ref="C"),
     tgt("temporal_cloud_v1_operations_count / clamp_min(temporal_cloud_v1_operations_limit, 1)", legend="operations", ref="D")],
    "percentunit",
    "The saturation signal that has no self-hosted equivalent, and the one that surprises teams migrating to Cloud.\n\nAlert on the RATIO so it survives a limit increase. Hitting a limit looks like an outage to your users while sitting inside Temporal's SLA — Throttling is explicitly excluded from it.",
    overrides=[ov("actions", C1), ov("requests", C2), ov("pollers", C3), ov("operations", C4)],
    minv=0, maxv=1.2, decimals=2,
    thresholds=thr([{"color": GOOD, "value": None}, {"color": SERIOUS, "value": 0.8},
                    {"color": CRITICAL, "value": 1.0}]), fill=0))

P.append(ts("Throttling", {"h": 8, "w": 8, "x": 8, "y": 34},
    [tgt(f"sum(avg_over_time(temporal_cloud_v1_total_action_throttled_count[{W}]))", legend="actions"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_operations_throttled_count[{W}]))", legend="operations", ref="B"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_service_request_throttled_count[{W}]))", legend="requests", ref="C"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_resource_exhausted_error_count[{W}]))", legend="resource exhausted", ref="D")],
    "reqps",
    "The moment your users start feeling a limit. Anything above zero is real.\n\nThis can be non-zero while the usage ratios look fine: metrics are per-second rates averaged over a minute, so a sub-minute burst throttles without moving the average. If they disagree, believe this panel.",
    overrides=[ov("actions", SERIOUS), ov("operations", C4),
               ov("requests", CRITICAL), ov("resource exhausted", C5)],
    thresholds=thr([{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 0.0001}]),
    fill=18))

P.append(ts("Task Queue backlog", {"h": 8, "w": 8, "x": 16, "y": 34},
    [tgt("topk(6, temporal_cloud_v1_approximate_backlog_count)",
         legend="{{temporal_task_queue}} / {{task_type}}"),
     tgt(f"sum(avg_over_time(temporal_cloud_v1_no_poller_tasks_count[{W}]))", legend="tasks with NO poller", ref="B")],
    "short",
    "Backlog is a VALUE (current depth), not a rate — a large but draining backlog is fine, a growing one is not.\n\n'tasks with no poller' is the signal with no false-positive mode: work is arriving on a queue nobody polls. On Cloud that is always yours — a Task Queue name mismatch or a Worker fleet that is down.",
    overrides=[ov("tasks with NO poller", CRITICAL)],
    legend="table", place="bottom", calcs=["lastNotNull", "max"]))

dash = {
    "__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
                  "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
    "annotations": {"list": []},
    "description": "Four golden signals for Temporal Cloud, with SLO attainment and error budget burn. Saturation is expressed against Cloud limits.",
    "editable": True, "graphTooltip": 1, "links": [], "panels": P, "preload": False,
    "refresh": "1m", "schemaVersion": 39,
    "tags": ["temporal", "temporal-cloud", "golden-signals", "sre", "slo"],
    "templating": {"list": [{"current": {}, "hide": 0, "includeAll": False,
                             "label": "Data source", "multi": False, "name": "DS_PROMETHEUS",
                             "options": [], "query": "prometheus", "refresh": 1,
                             "regex": "", "skipUrlSync": False, "type": "datasource"}]},
    "time": {"from": "now-6h", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Temporal Cloud — Golden Signals (RED + Saturation)",
    "uid": "temporal-cloud-golden-signals", "version": 1, "weekStart": "",
}

import pathlib
pathlib.Path(OUT).parent.mkdir(parents=True, exist_ok=True)
with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
