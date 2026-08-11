#!/usr/bin/env python3
"""Generate the Temporal SLO board dashboard."""
import json

import pathlib
OUT = str(pathlib.Path(__file__).resolve().parent.parent / "demo/grafana/dashboards/slo/temporal-slo-board.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

# ---------------------------------------------------------------------------
# Status palette. THREE states, not four, and the reason is measured.
#
# The reference status palette has four roles (good/warning/serious/critical).
# Validated against Grafana's dark panel surface (#181b1f), warning #fab219 and
# serious #ec835a sit at normal-vision ΔE 13.6 — below the 15 floor, i.e. hard
# to tell apart even with full colour vision. They would have been ADJACENT
# bands on this wall, so one of them had to go.
#
# The remaining three all clear 3:1 contrast on the dark surface.
#
# THE HONEST LIMITATION: good #0ca30c against serious #ec835a measures ΔE 5.6
# under protanopia. On a wall whose entire job is "spot the tile that is not
# green", a red-green colourblind viewer cannot do that by colour. This is not
# fixable by re-picking within a documented palette — it is why the status rule
# is "icon + label, never colour alone". Every tile therefore carries its SLI
# name AND its signed percentage, and the value is the channel that actually
# carries the meaning: negative means breached, full stop. The board table below
# is the table view for the same data.
STATUS_GOOD     = "#0ca30c"
STATUS_SERIOUS  = "#ec835a"
STATUS_CRITICAL = "#d03b3b"

_id = [0]
def nid():
    _id[0] += 1
    return _id[0]


def target(expr, legend=None, instant=False, fmt=None, ref="A"):
    t = {
        "datasource": DS,
        "editorMode": "code",
        "expr": expr,
        "range": not instant,
        "instant": instant,
        "refId": ref,
    }
    if legend is not None:
        t["legendFormat"] = legend
    if fmt:
        t["format"] = fmt
    return t


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def thresholds(steps):
    return {"mode": "absolute", "steps": steps}


# Same three states as the wall. The 6x band was dropped along with the warning
# colour: 1x is "spending faster than sustainable" and 14.4x is "page someone",
# and a fourth colour between them bought a confusable pair, not information.
burn_thr = thresholds([
    {"color": STATUS_GOOD, "value": None},
    {"color": STATUS_SERIOUS, "value": 1},
    {"color": STATUS_CRITICAL, "value": 14.4},
])


def panel(ptype, title, gp, targets, unit=None, desc=None, thr=None,
          minv=None, maxv=None, opts=None, overrides=None, decimals=None):
    defaults = {"color": {"mode": "thresholds" if thr else "palette-classic"}}
    if unit:
        defaults["unit"] = unit
    if thr:
        defaults["thresholds"] = thr
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    if decimals is not None:
        defaults["decimals"] = decimals
    p = {
        "type": ptype,
        "title": title,
        "id": nid(),
        "datasource": DS,
        "gridPos": gp,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
    }
    if desc:
        p["description"] = desc
    if opts:
        p["options"] = opts
    return p


panels = []

# =====================================================================
# ROW 1 — headline error budget status
# =====================================================================
panels.append(row("Error Budget — compliance window 1h", 0))

panels.append(panel(
    "stat", "SLOs in breach",
    {"h": 10, "w": 5, "x": 0, "y": 1},
    [target("count(slo:error_budget_remaining:ratio <= 0) or vector(0)", instant=True)],
    desc=("How many SLOs have fully spent their error budget. The one number to "
          "read first: zero means the wall is green and you can stop looking. "
          "Above zero is an error budget policy conversation, which is not "
          "automatically an incident — the budget exists to be spent."),
    thr=thresholds([
        {"color": STATUS_GOOD, "value": None},
        {"color": STATUS_CRITICAL, "value": 1},
    ]),
    opts={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
          "colorMode": "background", "graphMode": "none", "textMode": "value"},
))

wall = panel(
    "stat", "Error budget wall", {"h": 10, "w": 19, "x": 5, "y": 1},
    # clamp_min at -1 is a DISPLAY decision, not a fudge. A 1-hour compliance
    # window is small enough that one chaos scenario can burn 75x the budget:
    # measured after `make chaos-backlog`, workflow_completion read -7485%.
    # That number is unreadable and makes the panel look broken, and past -100%
    # it stops carrying useful information anyway — the budget is gone either
    # way, and "how fast" is what you act on, which is the burn rate panel below.
    [target("clamp_min(slo:error_budget_remaining:ratio, -1)",
            legend="{{sli}} {{service_name}}", instant=True)],
    unit="percentunit",
    desc=("One tile per SLO, coloured by error budget remaining. Healthy is a "
          "wall of green; a single tile changing colour is the whole point.\n\n"
          "GREEN  >25% of the budget left\n"
          "ORANGE 0-25% left — at risk, still meeting the SLO\n"
          "RED    budget spent; the SLO has been MISSED for this window\n\n"
          "Saturates at -100%: on a 1h window a bad scenario can burn many times "
          "the budget, and -7485% is noise. Once a tile is red the budget is "
          "gone; the burn rate panel tells you how fast and whether it is still "
          "happening.\n\n"
          "Read the number, not just the colour: green vs orange is close to "
          "indistinguishable for red-green colourblind viewers (protan ΔE 5.6), "
          "so the signed percentage is the reliable channel. Negative means "
          "breached."),
    minv=-1, maxv=1, decimals=1,
    thr=thresholds([
        {"color": STATUS_CRITICAL, "value": None},
        {"color": STATUS_SERIOUS, "value": 0.0001},
        {"color": STATUS_GOOD, "value": 0.25},
    ]),
    opts={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
          # background = the tile itself is the status colour. This is what makes
          # it a wall rather than a row of numbers.
          "colorMode": "background",
          "graphMode": "none",
          # value_and_name is the secondary encoding the status rule requires:
          # every tile states what it is and how much budget is left, so meaning
          # never rests on hue alone.
          "textMode": "value_and_name",
          "justifyMode": "center",
          "wideLayout": True,
          "orientation": "auto"},
)
# A no-traffic SLI has no value, and "No data" reads as breakage. It is not:
# you cannot compute a success ratio over zero events.
wall["fieldConfig"]["defaults"]["noValue"] = "no traffic"
panels.append(wall)

# =====================================================================
# ROW 2 — the board itself
# =====================================================================
panels.append(row("SLO Board — every Temporal service role", 11))

# NOTE ON SHAPE. These are plain instant queries, NOT format:"table".
#
# The earlier version used format:"table" plus a `merge` transformation and the
# conventional "Value #A" column names. That is the documented pattern and it
# did not work here: the Prometheus datasource returns one frame PER SERIES with
# the labels attached as frame metadata rather than as columns, so `merge` had
# no `sli` column to join on. The result was a table with misaligned rows and an
# empty "Budget left" column.
#
# `labelsToFields` promotes those label-metadata entries into real columns, and
# because each query returns a DIFFERENT metric name, the value columns are
# already uniquely named — no "Value #A" disambiguation to depend on, and
# nothing for merge to collide over. `merge` then joins on the shared
# sli/service_name columns.
# legendFormat sets each query's display name, which becomes the value COLUMN
# name after labelsToFields. Setting it explicitly removes the guesswork about
# whether the column arrives as "Value", "Value #A", or the raw metric name —
# all three are plausible depending on version, and guessing wrong is what
# produced an empty "Budget left" column.
board_targets = [
    target("slo:objective_expanded:ratio",      legend="Objective",      instant=True, ref="A"),
    target("slo:sli_good:ratio",                legend="Attained",       instant=True, ref="B"),
    target("clamp_min(slo:error_budget_remaining:ratio, -1)", legend="Budget left",    instant=True, ref="C"),
    target("slo:burn_rate:ratio_rate1h",        legend="Burn rate (1h)", instant=True, ref="D"),
]

board = panel(
    "table", "SLO board", {"h": 11, "w": 24, "x": 0, "y": 12}, board_targets,
    desc=("One row per SLO. Objective is the promise; Attained is what actually "
          "happened over the compliance window; Budget left is how much room "
          "remains; Burn rate is how fast it is being consumed right now "
          "(1.0 = exactly on pace to run out at the end of the window)."),
    unit="percentunit",
    thr=thresholds([{"color": "text", "value": None}]),
    overrides=[
        {"matcher": {"id": "byName", "options": "Burn rate (1h)"},
         "properties": [
             {"id": "unit", "value": "none"},
             {"id": "decimals", "value": 2},
             {"id": "custom.cellOptions",
              "value": {"type": "color-text"}},
             {"id": "thresholds", "value": burn_thr},
             {"id": "custom.align", "value": "right"},
         ]},
        {"matcher": {"id": "byName", "options": "Budget left"},
         "properties": [
             # Solid, not gradient: a gradient interpolates between threshold
             # colours, which invents intermediate states that do not exist.
             {"id": "custom.cellOptions",
              "value": {"type": "color-background", "mode": "basic"}},
             {"id": "custom.align", "value": "right"},
             {"id": "thresholds", "value": thresholds([
                 {"color": STATUS_CRITICAL, "value": None},
                 {"color": STATUS_SERIOUS, "value": 0.0001},
                 {"color": STATUS_GOOD, "value": 0.25},
             ])},
         ]},
        {"matcher": {"id": "byName", "options": "Attained"},
         "properties": [{"id": "decimals", "value": 4},
                        {"id": "custom.align", "value": "right"}]},
        {"matcher": {"id": "byName", "options": "Objective"},
         "properties": [{"id": "decimals", "value": 3},
                        {"id": "custom.align", "value": "right"}]},
        # Most SLIs have no service_name — only persistence_availability does.
        # Left raw, those cells render blank and the column reads as broken data
        # rather than "not applicable".
        {"matcher": {"id": "byName", "options": "Service role"},
         "properties": [
             {"id": "custom.align", "value": "left"},
             {"id": "custom.width", "value": 130},
             {"id": "mappings", "value": [
                 {"type": "special", "options": {
                     "match": "empty", "result": {"text": "—", "index": 0}}},
                 {"type": "special", "options": {
                     "match": "null", "result": {"text": "—", "index": 1}}},
             ]},
         ]},
        {"matcher": {"id": "byName", "options": "SLI"},
         "properties": [{"id": "custom.align", "value": "left"},
                        {"id": "custom.width", "value": 230}]},
    ],
)
board["transformations"] = [
    # 1. Labels (sli, service_name) become real columns.
    {"id": "labelsToFields", "options": {}},
    # 2. Drop Time and __name__ BEFORE merging.
    #    Merge joins on every shared column. Time can differ by a millisecond
    #    between queries, and labelsToFields also promotes __name__ — which is
    #    DIFFERENT for each of the four queries by definition. Either one turns
    #    what should be one row into four half-empty ones, which is exactly the
    #    misalignment and the blank "Budget left" column.
    {"id": "filterFieldsByName", "options": {"exclude": {"pattern": "^(Time|__name__)$"}}},
    # 3. Join the four frames on the only columns left in common: sli and,
    #    where present, service_name.
    {"id": "merge", "options": {}},
    # 4. Column order and friendly label names. The value columns are already
    #    named by legendFormat above.
    {"id": "organize", "options": {
        "excludeByName": {},
        # Rename by the RAW metric name. legendFormat does NOT become the column
        # name for table frames — verified by screenshot: the columns rendered as
        # "slo:objective_expan…", "slo:sli_good:ratio", "slo:error_budget_re…".
        # Both spellings are listed so it works whichever name arrives.
        "renameByName": {
            "sli": "SLI",
            "service_name": "Service role",
            "slo:objective_expanded:ratio": "Objective",
            "slo:sli_good:ratio": "Attained",
            # clamp_min() strips __name__, so this column arrives as plain "Value"
            # (verified against the datasource) while the other three keep their
            # metric names. Both spellings mapped so either shape lands correctly.
            "Value": "Budget left",
            "slo:error_budget_remaining:ratio": "Budget left",
            "slo:burn_rate:ratio_rate1h": "Burn rate (1h)",
        },
        "indexByName": {
            "sli": 0,
            "service_name": 1,
            "slo:objective_expanded:ratio": 2,
            "slo:sli_good:ratio": 3,
            "Value": 4,
            "slo:error_budget_remaining:ratio": 4,
            "slo:burn_rate:ratio_rate1h": 5,
        },
    }},
    # 5. Worst budget first — the row you need is at the top.
    {"id": "sortBy", "options": {"fields": {},
                                 "sort": [{"field": "Budget left", "desc": False}]}},
]
board["options"] = {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                               "fields": ""}}
panels.append(board)

# =====================================================================
# ROW 3 — burn rate over time
# =====================================================================
panels.append(row("Burn rate", 23))

panels.append(panel(
    "timeseries", "Error budget burn rate (1h window)",
    {"h": 9, "w": 12, "x": 0, "y": 24},
    [target("slo:burn_rate:ratio_rate1h", legend="{{sli}} {{service_name}}")],
    unit="none",
    desc=("Multiples of sustainable budget consumption. 1.0 exhausts the budget "
          "exactly at the end of the window. 14.4 exhausts it in about 2 hours of "
          "a 30-day window — the standard fast-burn paging threshold. Below 1.0 "
          "you are spending less than you are allowed."),
    thr=burn_thr,
    opts={"legend": {"displayMode": "table", "placement": "right",
                     "calcs": ["lastNotNull", "max"], "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}},
))

panels.append(panel(
    "timeseries", "Fast-burn detector (5m vs 1h)",
    {"h": 9, "w": 12, "x": 12, "y": 24},
    [target("slo:burn_rate:ratio_rate5m", legend="5m — {{sli}} {{service_name}}"),
     target("slo:burn_rate:ratio_rate1h", legend="1h — {{sli}} {{service_name}}", ref="B")],
    unit="none",
    desc=("SLOFastBurn pages only when BOTH windows exceed 14.4. The long window "
          "proves the burn is real; the short window proves it is still happening, "
          "so the alert clears promptly instead of hanging around for an hour "
          "after the incident ends."),
    thr=burn_thr,
    opts={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}},
))

# =====================================================================
# ROW 4 — per service role detail
# =====================================================================
panels.append(row("Per Temporal service role", 33))

panels.append(panel(
    "timeseries", "Request availability — frontend / history / matching",
    {"h": 9, "w": 12, "x": 0, "y": 34},
    [target('1 - slo:sli_bad:ratio_rate5m{sli=~".*_availability", sli!="persistence_availability"}',
            legend="{{sli}}")],
    unit="percentunit",
    desc=("Only these three roles serve gRPC traffic, so only they have a "
          "request-based availability SLI. Client-caused errors (Canceled, "
          "NotFound, InvalidArgument) are excluded — counting them measures "
          "caller behaviour, not service health."),
    decimals=3,
    opts={"legend": {"displayMode": "table", "placement": "bottom",
                     "calcs": ["lastNotNull", "min"], "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}},
))

panels.append(panel(
    "timeseries", "Persistence availability — all roles incl. worker & server",
    {"h": 9, "w": 12, "x": 12, "y": 34},
    [target('1 - slo:sli_bad:ratio_rate5m{sli="persistence_availability"}',
            legend="{{service_name}}")],
    unit="percentunit",
    desc=("The worker and server roles serve no gRPC traffic of their own, so "
          "persistence is how they get an SLO at all. Every role talks to the "
          "datastore, which also makes this the SLI that most often explains a "
          "latency breach elsewhere."),
    decimals=3,
    opts={"legend": {"displayMode": "table", "placement": "bottom",
                     "calcs": ["lastNotNull", "min"], "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}},
))

panels.append(panel(
    "timeseries", "Latency SLIs — served under 500ms (long-polls excluded)",
    {"h": 9, "w": 12, "x": 0, "y": 43},
    [target('1 - slo:sli_bad:ratio_rate5m{sli=~".*_latency"}', legend="{{sli}}")],
    unit="percentunit",
    desc=("PollWorkflowTaskQueue and PollActivityTaskQueue are excluded. They "
          "block for up to 60s by design and are the highest-volume operations "
          "on the Frontend, so including them makes this panel a measure of how "
          "long Workers sit idle rather than how fast the service is."),
    decimals=3,
    opts={"legend": {"displayMode": "table", "placement": "bottom",
                     "calcs": ["lastNotNull", "min"], "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}},
))

panels.append(panel(
    "timeseries", "Worker fleet & application SLIs",
    {"h": 9, "w": 12, "x": 12, "y": 43},
    [target('1 - slo:sli_bad:ratio_rate5m{sli="worker_task_delivery"}',
            legend="Activity Tasks started within 200ms"),
     target('1 - slo:sli_bad:ratio_rate5m{sli="workflow_completion"}',
            legend="Workflows completing successfully", ref="B")],
    unit="percentunit",
    desc=("worker_task_delivery is an SDK metric and disappears entirely when the "
          "Worker fleet is down — see TemporalWorkerFleetAbsent. "
          "workflow_completion comes from the history service, so it keeps "
          "reporting through a total Worker outage."),
    decimals=3,
    opts={"legend": {"displayMode": "table", "placement": "bottom",
                     "calcs": ["lastNotNull", "min"], "showLegend": True},
          "tooltip": {"mode": "multi", "sort": "desc"}},
))

# Burn rate spans three orders of magnitude in practice: 0 when healthy, the 1x
# and 14.4x decision lines, and 80-100x during a real storm (measured during
# make chaos-backlog). On a linear axis the storm flattens the decision lines
# into the baseline, which are the only two values anyone acts on. symlog keeps
# 0-1 linear — so healthy series stay visible instead of vanishing the way a
# pure log axis would drop zero — and compresses everything above.
for _p in panels:
    if _p["type"] == "timeseries" and "urn" in _p.get("title", ""):
        _p["fieldConfig"]["defaults"].setdefault("custom", {})["scaleDistribution"] = {
            "type": "symlog", "log": 10, "linearThreshold": 1,
        }

dashboard = {
    "__inputs": [{
        "name": "DS_PROMETHEUS", "label": "Prometheus", "description": "",
        "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus",
    }],
    "annotations": {"list": []},
    "description": ("Error budgets and burn rates for every Temporal service role. "
                    "Compliance window is 1 hour so a chaos scenario visibly drains "
                    "the budget — production would use 28-30 days."),
    "editable": True,
    "graphTooltip": 1,
    "links": [],
    "panels": panels,
    "preload": False,
    "refresh": "30s",
    "schemaVersion": 39,
    "tags": ["temporal", "slo", "error-budget", "sre"],
    "templating": {"list": [{
        "current": {},
        "hide": 0,
        "includeAll": False,
        "label": "Data source",
        "multi": False,
        "name": "DS_PROMETHEUS",
        "options": [],
        "query": "prometheus",
        "refresh": 1,
        "regex": "",
        "skipUrlSync": False,
        "type": "datasource",
    }]},
    "time": {"from": "now-1h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "title": "Temporal SLO Board — Error Budgets",
    "uid": "temporal-slo-board",
    "version": 1,
    "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

npanels = len([p for p in panels if p["type"] != "row"])
print(f"wrote {OUT}")
print(f"  {npanels} panels + {len([p for p in panels if p['type']=='row'])} rows")
