#!/usr/bin/env python3
"""Workers — Tuning: the board for "did that tuning change help?".

Companion to demo/lab/javaworker (the Java tuning feature flag) and
demo/k6/worker-tuning (the stepped load test). Laid out in the order you
diagnose, which is also the order the lab's diagnosis checks things:

    1. which tuning is LIVE           the flag, exported as metrics
    2. is work waiting?               schedule-to-start, server backlog
    3. slots                          used vs available, the tuner's view
    4. pollers                        count, poll latency, sync match
    5. Java Worker internals          cache vs Workflow threads, heap, CPU
    6. the load generator             was the load actually delivered?
    7. the server                     rule it out before tuning Workers

Every trap below was measured on the lab (demo/lab/README.md), not read:

- Task queue is labelled THREE ways. Java/Micrometer: task_queue="lab-java".
  Go/tally: task_queue="lab_go" (sanitised). Server: taskqueue="lab_java". So
  this board has a separate server-side variable; one variable joined across
  them matches nothing on one side, silently.
- Java <1.30 has NO num_pollers metric, and DescribeTaskQueue counts poller
  IDENTITIES (one per process), so a Worker with 2 Activity pollers read
  "pollers: 1". The flag's worker_tuning_setting is the only poller count on a
  1.2x board, and is labelled as configured, not measured.
- Under ResourceBasedTuner, 1.28.4 reports no ActivityWorker slots_available.
  With a plain Micrometer reporter the old value then FREEZES (measured: 10
  free slots shown while the tuner granted 1 and 300 tasks queued). The lab
  Worker uses GenerationalGaugeReporter so the series goes absent instead; the
  slot panel's description says what absent means.
- Go long-poll latency carries task_queue="none", so poll latency is filtered
  by job and accepts "none".
- temporal_sticky_cache_total_forced_eviction counts ordinary completions
  (measured: 185 = 185 Workflows completed). Not on this board on purpose.

Usage: python3 tools/generate_worker_tuning_dashboard.py [OUT.json]
"""
import json, sys, pathlib

# The only argument is an output path. A flag is refused rather than taken as a
# filename: `--help` once wrote the whole dashboard to a file named "--help".
if len(sys.argv) > 1 and sys.argv[1].startswith("-"):
    sys.exit(f"usage: {sys.argv[0]} [OUTPUT.json]  (default: demo/grafana/dashboards/custom/worker-tuning.json)")
OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent /
        "demo/grafana/dashboards/custom/worker-tuning.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
GOOD, WARN, BAD = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C4 = "#3987e5", "#d95926", "#199e70", "#c98500"

NS = 'namespace=~"$namespace"'
TQ = f'{NS}, task_queue=~"$task_queue"'
STQ = f'{NS}, taskqueue=~"$server_task_queue"'
JOB = 'job=~"$job"'
RI = "$__rate_interval"

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

def stat(title, gp, targets, unit, desc, thr, decimals=None, novalue=None, text="auto"):
    d = {"color": {"mode": "thresholds"}, "unit": unit, "thresholds": thr}
    if decimals is not None: d["decimals"] = decimals
    if novalue: d["noValue"] = novalue
    return {"type": "stat", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none",
                        "textMode": text, "justifyMode": "auto", "wideLayout": True}}

def ts(title, gp, targets, unit, desc, decimals=None, minv=0, maxv=None,
       style="line", interp="smooth", calcs=None, overrides=None):
    d = {"color": {"mode": "palette-classic"}, "unit": unit,
         "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never",
                    "spanNulls": False, "lineInterpolation": interp,
                    "drawStyle": style, "axisSoftMin": 0}}
    if decimals is not None: d["decimals"] = decimals
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    return {"type": "timeseries", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": d, "overrides": overrides or []},
            "options": {"legend": {"displayMode": "table" if calcs else "list",
                                   "placement": "bottom", "showLegend": True,
                                   "calcs": calcs or []},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}

def gp(x, y, w, h=8):
    return {"h": h, "w": w, "x": x, "y": y}

P = []
y = 0

P.append({"type": "text", "title": "", "id": nid(), "transparent": True, "gridPos": gp(0, y, 24, 3),
          "options": {"mode": "markdown", "content":
    "**Read top to bottom: it's the order you diagnose in.** Change ONE tuning value at a time "
    "(flip the flag), let k6 hold each step for several Workflow durations, and compare the step "
    "where schedule-to-start first rises. Swap markers show when a new Worker generation started. "
    "Rule out the server (bottom row) before tuning Workers. See `demo/lab/README.md` for the measured cases."}})
y += 3

# ---------------------------------------------------------------- 1. FLAG
P.append(row("1 · Which tuning is live (the feature flag)", y)); y += 1
P.append(stat("Tuning mode", gp(0, y, 5, 5),
    [tgt(f'max by (mode) (worker_tuning_mode{{{TQ}}}) == 1', "{{mode}}")],
    "none",
    "The flag's mode on the Workers serving this queue: MANUAL, RESOURCE_BASED or SDK_DEFAULTS.\n\n"
    "Exported by SwappableWorker (demo/lab/javaworker). Without the flag exported as a metric, "
    "'did the change help?' is answered from memory: nothing on the board says which config was "
    "live when the latency moved.\n\n"
    "More than one value at once means Workers on the same queue disagree, mid-rollout or "
    "because a pod never picked up the flag (a ConfigMap mounted with subPath never updates).",
    {"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    text="name", novalue="no flag metrics"))
P.append(stat("Flag rejected (range)", gp(5, y, 4, 5),
    [tgt(f'sum(increase(worker_tuning_flag_rejected_total{{{TQ}}}[$__range])) or vector(0)')],
    "short",
    "Flag values refused by validation, or swaps that failed to start. The previous config KEPT "
    "running, by design: a Worker that falls back to SDK defaults after a typo looks healthy and "
    "runs a config nobody chose.\n\nNon-zero means the config you think you shipped is NOT live. "
    "The Worker log has the reason (e.g. 0 means 'SDK default', not 'none'; RESOURCE_BASED on "
    "1.29-1.32 refused for the tuner hang bug).",
    {"mode": "absolute", "steps": [{"color": GOOD, "value": None}, {"color": BAD, "value": 1}]},
    decimals=0))
P.append(stat("Landmine warnings", gp(9, y, 4, 5),
    [tgt(f'max(worker_tuning_flag_warnings{{{TQ}}}) or vector(0)')],
    "short",
    "Warnings on the flag that is live: applied, but probably not what was meant. For example, "
    "sticky cache larger than the Workflow thread pool, more pollers than slots (a Java poller "
    "reserves a slot before it polls), or 1 WFT poller (Java raises it to 2).",
    {"mode": "absolute", "steps": [{"color": GOOD, "value": None}, {"color": WARN, "value": 1}]},
    decimals=0))
P.append(stat("Worker generations started (range)", gp(13, y, 4, 5),
    [tgt(f'sum(increase(worker_tuning_swaps_total{{{TQ}}}[$__range])) or vector(0)')],
    "short",
    "Each flag change that altered the Worker started a new WorkerFactory: Java cannot change a "
    "running Worker's options. Each swap starts with an EMPTY sticky cache, so every open Workflow "
    "replays once (measured: hit ratio 75% for one 10 s window, then 100%). Swaps are "
    "rate-limited (TUNING_MIN_SWAP_INTERVAL_S), so a flapping flag cannot thrash the cache.",
    {"mode": "absolute", "steps": [{"color": C1, "value": None}]}, decimals=0))
P.append(ts("Effective settings (configured, not measured)", gp(17, y, 7, 5),
    [tgt(f'max by (setting) (worker_tuning_setting{{{TQ}}})', "{{setting}}")],
    "short",
    "What the flag configured, after the SDK's own adjustments (WFT pollers 1 becomes 2).\n\n"
    "ON JAVA <1.30 THIS IS THE ONLY POLLER COUNT YOU HAVE. There is no num_pollers metric before "
    "1.30, and DescribeTaskQueue counts poller identities (one per process), not concurrent polls: "
    "measured, a Worker with 2 Activity pollers read 'pollers: 1'.",
    style="line", interp="stepAfter"))
y += 5

# ---------------------------------------------------------------- 2. WAITING
P.append(row("2 · Is work waiting?", y)); y += 1
for i, (kind, metric) in enumerate([("Workflow Task", "temporal_workflow_task_schedule_to_start_latency_seconds_bucket"),
                                     ("Activity", "temporal_activity_schedule_to_start_latency_seconds_bucket")]):
    P.append(ts(f"{kind} schedule-to-start p95", gp(i * 8, y, 8),
        [tgt(f'histogram_quantile(0.95, sum by (le, task_queue) (rate({metric}{{{TQ}}}[{RI}])))', "{{task_queue}}")],
        "s",
        f"How long a {kind} waited on the server for a poller. THE saturation signal, and the "
        "number the tuning has to move.\n\n"
        "Go/Java emit SECONDS (_seconds suffix); TypeScript/Python/.NET emit milliseconds with no "
        "suffix and do not appear here.\n\n"
        "JAVA HISTOGRAM CEILING: Micrometer's default timer histogram stops at 30 s (verified: a "
        "120 s sample lands in +Inf), so beyond 30 s this panel flatlines at 30 s. The lab Worker "
        "raises it to 10 min with a MeterFilter; copy that into your Workers."))
P.append(ts("Server backlog (sum across partitions)", gp(16, y, 8),
    [tgt(f'sum by (task_type) (approximate_backlog_count{{{STQ}}})', "{{task_type}}")],
    "short",
    "The server's own backlog gauge, summed across partitions. Use the SERVER task queue "
    "variable: the server sanitises the name (lab-java becomes lab_java).\n\n"
    "Cross-check against schedule-to-start, not alone: after a mass termination this gauge froze "
    "for minutes while the SDK measured 39 s waits.", decimals=0))
y += 8

# ---------------------------------------------------------------- 3. SLOTS
P.append(row("3 · Slots: is the Worker allowed to take more?", y)); y += 1
P.append(ts("Slots used / available", gp(0, y, 12),
    [tgt(f'sum by (worker_type) (temporal_worker_task_slots_used{{{TQ}, worker_type=~"WorkflowWorker|ActivityWorker"}})', "{{worker_type}} used"),
     tgt(f'sum by (worker_type) (temporal_worker_task_slots_available{{{TQ}, worker_type=~"WorkflowWorker|ActivityWorker"}})', "{{worker_type}} available", ref="B")],
    "short",
    "Available at 0 with CPU idle: slot-bound. Raise slots before adding Workers.\n\n"
    "UNDER RESOURCE_BASED there is no ActivityWorker 'available' line: 1.28.4 does not report it "
    "with a tuner. That absence is correct. With a plain Micrometer reporter the line instead "
    "FREEZES at the last MANUAL value (measured: 10 shown, 1 actually granted). Read the tuner "
    "panel to the right instead.", decimals=0))
P.append(ts("ResourceBasedTuner: what it sees", gp(12, y, 12),
    [tgt(f'max by (task_queue) (resource_slots_cpu_usage{{{JOB}}} or temporal_resource_slots_cpu_usage{{{JOB}}})', "system CPU (0-1)"),
     tgt(f'max by (task_queue) (resource_slots_mem_usage{{{JOB}}} or temporal_resource_slots_mem_usage{{{JOB}}})', "heap / -Xmx (0-1)", ref="B")],
    "percentunit",
    "The tuner's inputs. Before SDK 1.32 these have NO temporal_ prefix (resource_slots_*). "
    "Both names are queried here.\n\n"
    "CPU is getSystemCpuLoad(), the whole container or host, not this process, so a busy sidecar "
    "throttles your slots. Memory is JVM heap / -Xmx, garbage included, and blind to thread "
    "stacks and direct buffers.\n\n"
    "Measured: with target 60% the tuner held CPU near 0.6 by granting 1-2 of 50 slots while "
    "schedule-to-start grew past 23 s. It caps resource use, not latency. It is not an "
    "autoscaler. Empty in MANUAL mode.", maxv=1))
y += 8

# ---------------------------------------------------------------- 4. POLLERS
P.append(row("4 · Pollers: is the Worker asking often enough?", y)); y += 1
P.append(ts("Pollers: measured (1.30+/Go) and configured (flag)", gp(0, y, 8),
    [tgt(f'sum by (poller_type) (temporal_num_pollers{{{TQ}}})', "measured {{poller_type}}"),
     tgt(f'max by (setting) (worker_tuning_setting{{{TQ}, setting=~".*pollers"}})', "configured {{setting}}", ref="B")],
    "short",
    "Measured comes from num_pollers, which needs Java SDK 1.30+. Configured comes from the flag.\n\n"
    "Java SPLITS its WFT pollers between the sticky and normal queues, so configured 4 means 4 "
    "polls in total. Go polls sticky ADDITIONALLY, so the same number means more polls on Go.",
    style="line", interp="stepAfter", decimals=0))
P.append(ts("Poll latency p50 (long poll RPC)", gp(8, y, 8),
    [tgt(f'histogram_quantile(0.5, sum by (le, operation) (rate(temporal_long_request_latency_seconds_bucket{{{JOB}, {NS}, '
         f'task_queue=~"$task_queue|none", operation=~"Poll(Activity|Workflow)TaskQueue"}}[{RI}])))', "{{operation}}")],
    "s",
    "THE discriminator between poller-bound and server-bound, measured on the lab. With a backlog "
    "waiting:\n- polls return in 2-16 ms: the server had the task ready, and the Worker isn't "
    "asking often enough. Poller-bound, so raise pollers.\n- polls take 0.6-1.0 s: the server is "
    "slow to hand work out. More pollers won't help; check row 7.\n\nWith NO backlog, a long "
    "poll waits by design, so ignore this panel when nothing is queued.\n\n"
    "Go reports this metric with task_queue=\"none\", so it is filtered by Fleet (job), not by "
    "queue."))
P.append(ts("Sync match rate (server)", gp(16, y, 8),
    [tgt(f'sum by (task_type) (rate(poll_success_sync{{{STQ}}}[{RI}])) / '
         f'(sum by (task_type) (rate(poll_success{{{STQ}}}[{RI}])) > 0.1)', "{{task_type}}")],
    "percentunit",
    "Share of tasks handed straight to a waiting poller. Near 100% means pollers are waiting for "
    "work, and the Worker is not short of pollers.\n\nHidden below 0.1 dispatches/s: an idle "
    "queue's ratio is noise. Async match is poll_success - poll_success_sync. "
    "poll_timeouts is NOT async match.", maxv=1))
y += 8

# ---------------------------------------------------------------- 5. JAVA
P.append(row("5 · Java Worker internals: cache, threads, JVM", y)); y += 1
P.append(ts("Sticky cache: held vs configured, and Workflow threads", gp(0, y, 8),
    [tgt(f'sum(temporal_sticky_cache_size{{{TQ}}})', "cached Workflows"),
     # Factory-wide, so the SDK labels it task_queue="none": filter by job.
     tgt(f'sum(temporal_workflow_active_thread_count{{{JOB}, {NS}}})', "Workflow threads in use", ref="B"),
     tgt(f'max by (setting) (worker_tuning_setting{{{TQ}, setting=~"sticky_cache_size|max_workflow_threads"}})', "configured {{setting}}", ref="C")],
    "short",
    "THE JAVA TRAP. Each cached Workflow parks a Workflow thread, so the cache can never hold "
    "more than maxWorkflowThreads - 1, whatever its configured size.\n\nMeasured: cache 1000, "
    "20 threads: cached Workflows sat at 19, and 58% of Workflow Tasks missed and replayed, with "
    "slots free and CPU idle. Raising threads to 1000 took it to 40 cached (every open Workflow) "
    "and a 100% hit rate within 6 s.\n\nCached Workflows flat at threads - 1 while the configured "
    "cache is larger: raise threads, not the cache.", decimals=0))
P.append(ts("Sticky cache hit ratio, and forced thread evictions", gp(8, y, 8),
    [tgt(f'sum(rate(temporal_sticky_cache_hit_total{{{TQ}}}[{RI}])) / '
         # `or vector(0)` on the MISS side: the SDK creates the miss counter on the
         # first miss, so a perfect cache had no miss series and the ratio was
         # EMPTY — 100% hits rendered as No data (measured, resource-mode run).
         f'(sum(rate(temporal_sticky_cache_hit_total{{{TQ}}}[{RI}])) + (sum(rate(temporal_sticky_cache_miss_total{{{TQ}}}[{RI}])) or vector(0)) > 0)', "hit ratio"),
     tgt(f'sum(rate(temporal_sticky_cache_thread_forced_eviction_total{{{TQ}}}[{RI}])) or vector(0)', "thread-forced evictions /s", ref="B")],
    "short",
    "Hit ratio below about 80% means most Workflow Tasks replay history. Cheap on short histories "
    "(replay p95 under 1 ms here), expensive on long ones.\n\nThread-forced evictions UNDERSTATE "
    "the thread trap: measured 0.4/s while misses ran at 10/s. Most misses came from the "
    "capped cache, not from evictions, so judge by the left panel, not by this line.\n\n"
    "Do NOT alert on temporal_sticky_cache_total_forced_eviction: it counts ordinary "
    "completions (measured 1:1 with completed Workflows).",
    overrides=[{"matcher": {"id": "byName", "options": "hit ratio"},
                "properties": [{"id": "unit", "value": "percentunit"}, {"id": "max", "value": 1}]}]))
P.append(ts("JVM: process CPU, heap, live threads", gp(16, y, 8),
    [tgt(f'max by (instance) (process_cpu_usage{{{JOB}}})', "process CPU {{instance}}"),
     tgt(f'sum by (instance) (jvm_memory_used_bytes{{{JOB}, area="heap"}}) / sum by (instance) (jvm_memory_max_bytes{{{JOB}, area="heap"}} > 0)', "heap / max {{instance}}", ref="B"),
     tgt(f'max by (instance) (jvm_threads_live_threads{{{JOB}}})', "live threads {{instance}}", ref="C")],
    "percentunit",
    "Check CPU BEFORE pollers: a CPU-starved Worker looks poller-bound, because its pollers are "
    "starved too (measured on the Go lane).\n\nLive threads: every busy Activity slot and every "
    "cached Workflow is a platform thread, with its stack OUTSIDE the heap. Size the container "
    "for threads × stack, not just -Xmx. During a flag swap both generations' threads exist "
    "together.\n\nprocess_cpu_usage is a fraction of ALL cores visible to the JVM.",
    overrides=[{"matcher": {"id": "byRegexp", "options": "live threads.*"},
                "properties": [{"id": "unit", "value": "short"}, {"id": "custom.axisPlacement", "value": "right"}]}]))
y += 8

# ---------------------------------------------------------------- 6. K6
P.append(row("6 · Load generator (k6): was the load actually delivered?", y)); y += 1
P.append(ts("Starts / s by step", gp(0, y, 8),
    [tgt(f'sum by (step) (rate(k6_temporal_starts_ok_total[{RI}]))', "{{step}}/s step")],
    "reqps",
    "Workflow starts the SERVER accepted, from demo/k6/worker-tuning (remote write). A step that "
    "doesn't reach its target rate is a test that didn't deliver its load: read nothing about "
    "Worker capacity from it.\n\nEmpty if k6 ran without `-o experimental-prometheus-rw`."))
P.append(ts("Starts throttled / failed", gp(8, y, 8),
    [tgt(f'sum(rate(k6_temporal_starts_throttled_total[{RI}])) or vector(0)', "throttled (RESOURCE_EXHAUSTED)"),
     tgt(f'sum by (code) (rate(k6_temporal_starts_failed_total[{RI}]))', "failed code={{code}}", ref="B")],
    "reqps",
    "Throttled means a namespace RPS/APS limit (Cloud) or a frontend rate limit (self-hosted). The "
    "load never reached the Workers, so fix the limit first. k6 creates these counters on the "
    "first event, so 'throttled' falls back to 0 rather than No data."))
P.append(ts("Start RPC latency (k6 view)", gp(16, y, 8),
    [tgt('max(k6_temporal_start_latency_p99)', "p99"), tgt('max(k6_temporal_start_latency_p95)', "p95", ref="B")],
    "ms",
    "StartWorkflowExecution as seen from the load generator, network included. It measures the "
    "server, NOT your Workers: starts stay fast while Workers fall arbitrarily behind. Worker "
    "capacity is row 2 and the k6 teardown count of Workflows still Running."))
y += 8

# ---------------------------------------------------------------- 7. SERVER
P.append(row("7 · Server: rule it out before tuning Workers", y)); y += 1
P.append(ts("Persistence latency p95", gp(0, y, 12),
    [tgt(f'histogram_quantile(0.95, sum by (le) (rate(persistence_latency_bucket[{RI}])))', "persistence p95")],
    "s",
    "Self-hosted only. Measured on the lab: 2.7-4.6 ms healthy, 96 ms with Postgres CPU-capped "
    "(make lab-slow-db). A slow server holds slots open until completion RPCs return, which "
    "reads as slot-bound. Check this BEFORE raising slots.\n\nServer CPU alone is NOT evidence: "
    "at 240% server CPU the Workers were poller-bound, and the polls returned in 8-16 ms."))
P.append(ts("Task completion RPC p95 (frontend)", gp(12, y, 12),
    [tgt(f'histogram_quantile(0.95, sum by (le, operation) (rate(service_latency_bucket{{operation=~"RespondActivityTaskCompleted|RespondWorkflowTaskCompleted"}}[{RI}])))', "{{operation}}")],
    "s",
    "Measured: 32-48 ms healthy, 2-4 s server-bound. On Temporal Cloud, use the Cloud board "
    "instead: these are self-hosted server metrics."))
y += 8


def var_query(name, label, query, include_all=True, all_value=".*", default=None, desc=""):
    v = {"current": default or ({"selected": True, "text": ["All"], "value": ["$__all"]} if include_all else {}),
         "datasource": DS, "definition": query, "hide": 0, "includeAll": include_all,
         "label": label, "multi": True, "name": name, "options": [],
         "query": {"qryType": 1, "query": query, "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query", "description": desc}
    if include_all: v["allValue"] = all_value
    return v


dash = {
    "annotations": {"list": [
        {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"}, "enable": True,
         "hide": True, "iconColor": "rgba(0, 211, 255, 1)", "name": "Annotations & Alerts", "type": "dashboard"},
        # One marker per Worker generation. The question this board answers is
        # "before vs after the flip", so the flip has to be on every panel.
        {"datasource": DS, "enable": True, "iconColor": "#c98500", "name": "Tuning flag swap",
         "expr": 'changes(worker_tuning_swaps_total{namespace=~"$namespace", task_queue=~"$task_queue"}[1m]) > 0',
         "step": "30s", "titleFormat": "Tuning swap", "textFormat": "{{task_queue}} new Worker generation"},
    ]},
    "description": "Worker tuning: the flag that is live, then waiting, slots, pollers, Java internals, load delivered, server.",
    "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1,
    "links": [
        {"asDropdown": False, "icon": "dashboard", "tags": [], "targetBlank": False,
         "title": "Scale & Capacity", "tooltip": "fleet sizing", "type": "link", "url": "/d/temporal-worker-scale"},
        {"asDropdown": False, "icon": "external link", "tags": [], "targetBlank": True,
         "title": "Tuning lab", "tooltip": "live UI (make lab)", "type": "link", "url": "http://localhost:8095"},
    ],
    "panels": P, "preload": False, "refresh": "10s", "schemaVersion": 39,
    "tags": ["temporal", "workers", "tuning", "java"],
    "templating": {"list": [
        {"current": {}, "hide": 0, "includeAll": False, "label": "Data source", "multi": False,
         "name": "DS_PROMETHEUS", "options": [], "query": "prometheus", "refresh": 1, "regex": "",
         "skipUrlSync": False, "type": "datasource"},
        var_query("job", "Fleet (scrape job)", 'label_values(up{job=~"temporal-sdk.*"}, job)',
                  all_value="temporal-sdk.*"),
        var_query("namespace", "Namespace",
                  'label_values(temporal_worker_task_slots_available{namespace!~"temporal_system|system|_unknown_"}, namespace)',
                  all_value='[^_].*'),
        var_query("task_queue", "Task queue (SDK)",
                  'label_values(temporal_worker_task_slots_available{namespace=~"$namespace", task_queue!~"temporal_sys.*|none"}, task_queue)',
                  desc="As the SDK labels it. Java keeps dashes (lab-java); Go sanitises them (lab_go)."),
        var_query("server_task_queue", "Task queue (server)",
                  'label_values(approximate_backlog_count{namespace=~"$namespace", taskqueue!~"temporal_sys.*"}, taskqueue)',
                  all_value='[^t].*|t[^e].*',
                  desc="As the SERVER labels it: dashes become underscores (lab-java → lab_java). "
                       "A separate variable on purpose: one variable joined across both matches nothing on one side."),
    ]},
    "time": {"from": "now-30m", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "Workers — Tuning", "uid": "temporal-worker-tuning", "version": 1, "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type'] not in ('row', 'text')])} panels, {len([p for p in P if p['type'] == 'row'])} rows")
