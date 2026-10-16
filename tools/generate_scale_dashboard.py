#!/usr/bin/env python3
"""Worker Scale & Capacity — the board for a sizing question, not an incident.

WHAT THIS ANSWERS THAT THE GOLDEN-SIGNALS BOARD DOES NOT

RECOMMENDATIONS.md §4 documents a decision tree for rising schedule-to-start:
too few Workers, or each Worker configured too small. The two are
INDISTINGUISHABLE on a schedule-to-start panel, and the tree splits on host CPU
to separate them.

That diagnosis is correct and it is still made mid-incident, from a chart, under
pressure. This board puts the three quantities the tree needs side by side and
lays out the branches explicitly, so the answer is read rather than derived:

    offered load vs throughput   is the fleet keeping up at all?
    slots available (MINIMUM)    is it slot-bound?
    throughput per worker        is another Worker worth anything?

THE MINIMUM, NOT THE MEAN — the one design decision here that matters.

Slot exhaustion is a transient. A fleet that hits zero free slots for two
seconds in every ten is dropping throughput on the floor and has a completely
comfortable-looking average. Every slot panel on this board uses min_over_time,
and a mean would make the board agree with a healthy reading while the fleet
is losing work.

THE COUNTERPART TO scripts/scale-benchmark.sh. The script produces a table
across fleet sizes; this shows the same quantities live, for the fleet you have
now. Use the script to plan, this to verify.
"""
import json, sys, pathlib

OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent /
        "demo/grafana/dashboards/custom/worker-scale.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

GOOD, WARN, BAD = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C4, C5 = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
TEMPORAL_INDIGO = "#444CE7"

# Temporal's own internal Workers and namespace, excluded for the same measured
# reason as everywhere else in this repo: including temporal_system means every
# aggregate carries activity unrelated to the offered load, and absence alerts
# never fire.
TEN = 'namespace=~"$namespace", namespace!~"temporal_system|system|_unknown_"'
POLL = TEN + ', taskqueue!~"temporal_sys_.*"'

# $role selects the FLEET: order Workers or agent Workers. They are separate
# scrape jobs and must stay separately selectable — a blended figure is
# meaningless here, because the two have completely different healthy slot
# utilisation. An agent Worker is IO-blocked for seconds per task and runs
# comfortably at 80% slots busy; an order Worker at 80% is close to trouble.
FLEET = 'job=~"$role"'

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
# HERO STRIP
# =========================================================================
P.append({
    "type": "text", "title": "", "id": nid(), "transparent": True,
    "gridPos": {"h": 5, "w": 3, "x": 0, "y": 0},
    "options": {"mode": "html", "code": {"language": "plaintext",
                                         "showLineNumbers": False, "showMiniMap": False},
                "content": f'''<div style="background:{TEMPORAL_INDIGO};border-radius:3px;
  height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:0.6em;text-align:center;color:#fff;">
  <div style="font-size:15px;font-weight:700;letter-spacing:.02em;">Scale</div>
  <div style="font-size:11px;opacity:.85;margin-top:2px;">capacity &amp; slots</div>
</div>'''}})

P.append(stat("Workers polling", {"h": 5, "w": 4, "x": 3, "y": 0},
    # `or vector(0)`: count() of nothing is NO DATA, so with every Worker
    # down this stat read "No data" instead of 0 — exactly when it matters.
    [tgt(f'count(up{{{FLEET}}} == 1) or vector(0)')],
    "short",
    "Worker processes Prometheus is currently scraping.\n\n"
    "Discovered by dns_sd against the Compose service name, so it tracks "
    "`--scale` automatically. A static target would pin to whichever replica DNS "
    "resolved first and silently under-report the fleet — which is the kind of gap "
    "that makes a dashboard lie during an incident.\n\n"
    "If this disagrees with what you scaled to, dns_sd has not refreshed yet (10s) "
    "or a container failed to start. Every per-worker figure on this board divides "
    "by THIS number, not by what you asked for.",
    thr={"mode": "absolute", "steps": [{"color": BAD, "value": None},
                                       {"color": GOOD, "value": 1}]},
    decimals=0, novalue="none"))

P.append(stat("Throughput", {"h": 5, "w": 4, "x": 7, "y": 0},
    [tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval]))')],
    "reqps",
    "Successful Workflow completions per second — what the fleet is ACHIEVING.\n\n"
    "Compare against offered load. A persistent gap between the two is backlog "
    "being built, and the backlog is where the diagnosis lives.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=2))

P.append(stat("Throughput per worker", {"h": 5, "w": 4, "x": 11, "y": 0},
    [tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval])) / '
         f'clamp_min(count(up{{{FLEET}}} == 1) or vector(0), 1)')],
    "reqps",
    "Completions per second divided by the number of Workers actually scraped.\n\n"
    "THE NUMBER THAT TELLS YOU WHETHER ANOTHER WORKER IS WORTH BUYING. Watch it "
    "across a scale change: if it holds steady as you add Workers, the fleet is "
    "scaling linearly and horizontal scaling is the right lever. If it FALLS, each "
    "added Worker is buying less than the last and you are near the knee — the "
    "bottleneck has moved somewhere else (per-Worker concurrency, the server, or the "
    "load generator).\n\n"
    "scripts/scale-benchmark.sh measures exactly this across fleet sizes and reports "
    "it as an efficiency ratio.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None}]},
    decimals=2))

P.append(stat("Min activity slots free", {"h": 5, "w": 4, "x": 15, "y": 0},
    [tgt(f'min_over_time(sum(temporal_worker_task_slots_available{{worker_type="ActivityWorker", {FLEET}}})[$__range:])')],
    "short",
    "The LOWEST number of free activity slots anywhere in the fleet over the window.\n\n"
    "The minimum, not the mean, and that is the entire point of this panel. Slot "
    "exhaustion is a transient — a fleet that hits zero for two seconds in every ten "
    "is losing throughput and has a perfectly healthy average.\n\n"
    "ZERO means slot-bound: there was a moment with no capacity to accept a task "
    "anywhere. Raise MAX_CONCURRENT_ACTIVITIES before adding Workers; this is the "
    "branch of the RECOMMENDATIONS.md §4 tree that more Workers do not fix.\n\n"
    "A HIGH value with high schedule-to-start is the opposite finding — slots are "
    "free and tasks are still waiting, so the constraint is elsewhere.",
    thr={"mode": "absolute", "steps": [{"color": BAD, "value": None},
                                       {"color": WARN, "value": 1},
                                       {"color": GOOD, "value": 5}]},
    decimals=0, novalue="no workers"))

P.append(stat("Schedule-to-start p95", {"h": 5, "w": 5, "x": 19, "y": 0},
    [tgt(f'histogram_quantile(0.95, sum by (le) '
         f'(rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))')],
    "s",
    "How long an activity task waits for a poller.\n\n"
    "THE SATURATION SIGNAL, and the right thing to autoscale on — it measures the "
    "queue, not the host, so it rises for a fleet that is too small regardless of "
    "why. `aws/k8s/worker/hpa.yaml` scales on this rather than on CPU for exactly "
    "that reason: an IO-blocked agent Worker at 100% slot utilisation shows almost "
    "no CPU, so a CPU-based autoscaler never fires.\n\n"
    "The _seconds suffix is Go/Java SDK. TypeScript, Python and .NET emit "
    "MILLISECONDS with no suffix — see `make verify-sdk-labels`.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": WARN, "value": 0.5},
                                       {"color": BAD, "value": 2}]},
    decimals=3, novalue="no tasks"))

# =========================================================================
# ROW A — CAPACITY
# =========================================================================
P.append(row("A · Is the fleet keeping up?", 5))

P.append(ts("Offered load vs throughput", {"h": 8, "w": 12, "x": 0, "y": 6},
    [tgt(f'(sum(rate(workflow_success{{{TEN}}}[$__rate_interval])) or vector(0)) + '
         f'(sum(rate(workflow_failed{{{TEN}}}[$__rate_interval])) or vector(0)) + '
         f'(sum(rate(workflow_timeout{{{TEN}}}[$__rate_interval])) or vector(0))',
         "terminated (all outcomes)"),
     tgt(f'sum(rate(workflow_success{{{TEN}}}[$__rate_interval]))', "succeeded", ref="B"),
     # `or vector(0)`: temporal_workflow_failed_total does not exist until
     # the first failure, so on a healthy stack this target was empty and
     # `make validate` failed the whole panel — every time, not flakily.
     tgt(f'sum(rate(temporal_workflow_failed_total{{{TEN}}}[$__rate_interval])) or vector(0)',
         "SDK-observed failures", ref="C")],
    "reqps",
    "Work finishing, split by outcome. The gap between the top line and the offered "
    "rate is backlog being built.\n\n"
    "THE THREE TERMINAL COUNTERS ARE workflow_success, workflow_failed AND "
    "workflow_timeout — verified against this cluster, not assumed.\n\n"
    "EACH TERM IS GUARDED WITH `or vector(0)`, and that is load-bearing rather than "
    "defensive. Three drafts of this panel failed, each differently, and all three "
    "were caught by tools/check_dashboard_queries.py:\n\n"
    "  1. `workflow_completed` — does not exist on this server. Empty, no error.\n"
    "  2. `a + b + c` unguarded — returns EMPTY, not a partial sum, the moment any "
    "one operand has no samples. On a healthy cluster workflow_timeout has none, so "
    "the panel went dark precisely BECAUSE nothing was timing out.\n"
    "  3. `sum(rate({__name__=~\"a|b|c\"}))` — the obvious fix, and it does not work "
    "at all: rate() strips __name__, so the three metrics collapse to identical "
    "labelsets and Prometheus fails with 'vector cannot contain metrics with the "
    "same labelset'. No aggregation can rescue it, because the collision happens "
    "before any aggregation runs.\n\n"
    "Same family as `count()` over an empty vector returning no data rather than "
    "zero, which is documented on the SLO board and fixed the same way.\n\n"
    "There is also no server-side 'workflows started' counter, so arrivals cannot be "
    "plotted directly — these terminal counters lag arrivals by the execution "
    "duration. Adequate for a sustained rate, useless for spotting a burst. For the "
    "exact offered rate read it off the k6 run, which is why scale-benchmark.sh "
    "records it as its own column rather than inferring it.",
    fill=0, decimals=2, legend_mode="table", calcs=["lastNotNull", "mean"]))

P.append(ts("Schedule-to-start distribution", {"h": 8, "w": 12, "x": 12, "y": 6},
    [tgt(f'histogram_quantile(0.50, sum by (le) '
         f'(rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', "p50"),
     tgt(f'histogram_quantile(0.95, sum by (le) '
         f'(rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', "p95", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le) '
         f'(rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))', "p99", ref="C")],
    "s",
    "Queue wait at three quantiles.\n\n"
    "The SHAPE matters more than any single value. p50 near zero with a high p99 is "
    "a fleet that is mostly fine and occasionally starved — usually a burst arriving "
    "faster than the pollers pick up, which more Workers fix. All three rising "
    "together is sustained under-capacity.\n\n"
    "IF THIS LINE IS FLAT AND HIGH, suspect ScheduleToStartTimeout is set: it "
    "TRUNCATES this histogram at the timeout value, so a badly backed-up queue "
    "reports a flat ceiling instead of a rising line. The demo Workflow deliberately "
    "leaves it unset — see the comment in demo/app/workflow.go.",
    thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                       {"color": WARN, "value": 0.5},
                                       {"color": BAD, "value": 2}]},
    decimals=3, legend_mode="table", calcs=["lastNotNull", "max"]))

# =========================================================================
# ROW B — THE DIAGNOSIS
# =========================================================================
P.append(row("B · Slot-bound, worker-bound, or server-bound?", 14))

P.append(ts("Activity slots available per worker", {"h": 8, "w": 12, "x": 0, "y": 15},
    [tgt(f'min_over_time(sum by (instance) '
         f'(temporal_worker_task_slots_available{{worker_type="ActivityWorker", {FLEET}}})[$__interval:])',
         "{{instance}}")],
    "short",
    "Free activity slots, per Worker process, as a per-interval MINIMUM.\n\n"
    "Split by instance rather than summed because the distribution is the finding. "
    "All Workers near zero together is genuine fleet-wide saturation. ONE Worker at "
    "zero while others sit idle is a very different problem — usually uneven task "
    "routing, a sticky cache imbalance, or one host that is slower than the rest — "
    "and adding Workers does not fix it.\n\n"
    "Any line touching zero means that Worker had no free slot and was refusing "
    "work. A fleet-wide floor at zero is the slot-bound diagnosis.",
    thr={"mode": "absolute", "steps": [{"color": BAD, "value": None},
                                       {"color": WARN, "value": 1},
                                       {"color": GOOD, "value": 5}]},
    decimals=0, legend_mode="table", calcs=["min", "lastNotNull"]))

P.append(ts("Sync match rate and poll success", {"h": 8, "w": 12, "x": 12, "y": 15},
    # Scoped to tenant namespaces and away from temporal_sys_* queues, which
    # long-poll constantly and match nothing (CLAUDE.md): unscoped, poll
    # success sat permanently low and barely moved as the fleet grew.
    [tgt(f'sum(rate(poll_success_sync{{{POLL}}}[$__rate_interval])) / '
         f'clamp_min(sum(rate(poll_success{{{POLL}}}[$__rate_interval])), 0.0001)', "sync match rate"),
     tgt(f'sum(rate(poll_success{{{POLL}}}[$__rate_interval])) / '
         f'clamp_min(sum(rate(poll_success{{{POLL}}}[$__rate_interval])) + '
         f'sum(rate(poll_timeouts{{{POLL}}}[$__rate_interval])), 0.0001)', "poll success rate", ref="B")],
    "percentunit",
    "Two server-side rates that move in OPPOSITE directions as the fleet grows, "
    "which is what makes them useful together.\n\n"
    "SYNC MATCH RATE — the share of tasks handed straight to a waiting poller "
    "without a persistence round trip. Falls when the fleet is too small: tasks "
    "arrive with nobody waiting, so they get written to the queue and read back.\n\n"
    "POLL SUCCESS RATE — the share of long-polls that returned a task rather than "
    "timing out. Falls when the fleet is too LARGE: pollers outnumber tasks and most "
    "polls come back empty.\n\n"
    "SO: sync match low + poll success high = under-provisioned. Sync match at 1.0 + "
    "poll success collapsing = over-provisioned, which is measured in scenario 8 "
    "(poller flood: poll success 0.9995 → 0.6812 while sync match IMPROVED to 1.0000). "
    "Both at once is the sweet spot, and the reason TemporalMatchingStarved requires "
    "schedule-to-start as a second condition — poll success alone fires on a healthy "
    "over-provisioned cluster.",
    thr={"mode": "absolute", "steps": [{"color": BAD, "value": None},
                                       {"color": WARN, "value": 0.8},
                                       {"color": GOOD, "value": 0.95}]},
    fill=0, decimals=4, minv=0, maxv=1, legend_mode="table",
    calcs=["lastNotNull", "min"]))

# =========================================================================
# ROW C — AGENT FLEETS
#
# A row specifically for the fleet whose healthy utilisation looks broken.
# =========================================================================
P.append(row("C · Agent fleets — why CPU autoscaling fails here", 23))

P.append(ts("Activity execution latency by type", {"h": 8, "w": 12, "x": 0, "y": 24},
    [tgt(f'histogram_quantile(0.95, sum by (le, activity_type) '
         f'(rate(temporal_activity_execution_latency_seconds_bucket{{{TEN}}}[$__rate_interval])))',
         "{{activity_type}} p95")],
    "s",
    "How long each Activity type takes to execute, p95.\n\n"
    "This is the number that sets how many slots a Worker needs. One slot sustains "
    "roughly 1/latency activities per second, so:\n\n"
    "  50ms activity   → ~20/sec per slot  → 10 slots ≈ 200/sec\n"
    "  5s LLM call     → ~0.2/sec per slot → 10 slots ≈ 2/sec\n\n"
    "A hundred-fold difference in required concurrency for the same throughput. That "
    "is why the agent Worker is configured with 50 slots against the order Worker's "
    "10, and why a single default across both fleets is wrong for one of them.\n\n"
    "InvokeModel appearing here at seconds while ChargePayment sits at milliseconds "
    "is the whole argument for separate task queues.",
    fill=0, decimals=3, legend_mode="table", calcs=["lastNotNull", "max"]))

P.append(ts("Slot utilisation vs CPU — the autoscaling trap", {"h": 8, "w": 12, "x": 12, "y": 24},
    [tgt(f'1 - (sum(temporal_worker_task_slots_available{{worker_type="ActivityWorker", {FLEET}}}) / '
         f'clamp_min(sum(max_over_time(temporal_worker_task_slots_available{{worker_type="ActivityWorker", {FLEET}}}[$__range:])), 1))',
         "slot utilisation (approx)"),
     tgt(f'avg(rate(container_cpu_usage_seconds_total{{name=~".*worker.*"}}[$__rate_interval]))',
         "cpu cores per worker", ref="B")],
    "percentunit",
    "Slot utilisation beside CPU, to show that the two DISAGREE for an IO-bound "
    "fleet — which is the reason to autoscale on schedule-to-start rather than CPU.\n\n"
    "An agent Worker spends nearly all its time blocked on a provider's HTTP "
    "response. It can sit at 100% slots busy — completely unable to accept another "
    "task — while showing a few percent CPU. A CPU-based autoscaler sees an idle "
    "fleet and never scales; the queue backs up and schedule-to-start climbs with "
    "nothing on a CPU chart to explain it.\n\n"
    "SLOT UTILISATION HERE IS AN APPROXIMATION and honest about it: the SDK exports "
    "slots AVAILABLE, not slots CONFIGURED, so the denominator is inferred from the "
    "high-water mark over the window. That is only correct if the fleet was idle at "
    "some point in it — on a permanently busy fleet this READS LOW. The authoritative "
    "figure is MAX_CONCURRENT_ACTIVITIES, which the app knows and the metric does not.\n\n"
    "The CPU line needs cAdvisor and is absent on this stack by default. A missing "
    "line is the honest rendering; a fabricated zero would read as 'idle' and argue "
    "for exactly the wrong conclusion.",
    fill=0, decimals=3, minv=0, legend_mode="table", calcs=["lastNotNull", "max"]))

# =========================================================================
dash = {
    "annotations": {"list": []},
    "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1,
    "links": [
        {"asDropdown": False, "icon": "dashboard", "tags": [], "targetBlank": False,
         "title": "Golden signals", "tooltip": "RED + saturation",
         "type": "link", "url": "/d/temporal-golden-signals"},
        {"asDropdown": False, "icon": "dashboard", "tags": [], "targetBlank": False,
         "title": "AI agents", "tooltip": "GenAI cost and behaviour",
         "type": "link", "url": "/d/ai-agent-observability"},
    ],
    "panels": P, "preload": False,
    "refresh": "30s", "schemaVersion": 39,
    "tags": ["temporal", "capacity", "scale", "workers"],
    "templating": {"list": [
        {"current": {}, "hide": 0, "includeAll": False, "label": "Data source",
         "multi": False, "name": "DS_PROMETHEUS", "options": [], "query": "prometheus",
         "refresh": 1, "regex": "", "skipUrlSync": False, "type": "datasource"},
        # Fleet selector. Defaults to the ORDER Worker job rather than All,
        # because a blended slot figure across two fleets with different healthy
        # utilisation is not a number anyone should read.
        {"current": {"selected": True, "text": ["temporal-sdk-worker"],
                     "value": ["temporal-sdk-worker"]},
         "datasource": DS,
         "definition": 'label_values(up{job=~"temporal-sdk.*"}, job)',
         "hide": 0, "includeAll": True, "allValue": "temporal-sdk.*", "label": "Fleet",
         "multi": True, "name": "role", "options": [],
         "query": {"qryType": 1, "query": 'label_values(up{job=~"temporal-sdk.*"}, job)',
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
        {"current": {"selected": True, "text": ["All"], "value": ["$__all"]},
         "datasource": DS, "definition": "label_values(workflow_success, namespace)",
         "hide": 0, "includeAll": True, "allValue": ".*", "label": "Namespace",
         "multi": True, "name": "namespace", "options": [],
         "query": {"qryType": 1, "query": "label_values(workflow_success, namespace)",
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
    ]},
    "time": {"from": "now-30m", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Workers — Scale & Capacity",
    "uid": "temporal-worker-scale",
    "version": 1, "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
