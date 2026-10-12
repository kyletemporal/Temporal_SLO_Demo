#!/usr/bin/env python3
"""AI Agent Observability — the GenAI board.

EVERY METRIC NAME BELOW WAS READ OFF A RUNNING COLLECTOR, NOT DERIVED.

That is not a boast, it is the reason the board is not empty. The OTel
Prometheus exporter builds a series name from the instrument name AND its unit,
and the unit rules are not what you would guess:

    instrument                        unit          exported series
    ----------------------------------------------------------------------
    gen_ai.client.operation.duration  s             ..._duration_seconds
    gen_ai.client.token.usage         {token}       gen_ai_client_token_usage
    gen_ai.client.cost                {USD}         gen_ai_client_cost_total
    gen_ai.agent.tool.calls           {call}        gen_ai_agent_tool_calls_total
    gen_ai.agent.loop.iterations      {iteration}   gen_ai_agent_loop_iterations

`s` becomes a `_seconds` suffix. An ANNOTATION unit — anything in braces — is
dropped entirely. So the token histogram is `gen_ai_client_token_usage`, with no
unit suffix at all, while duration gets one. Writing `..._token_usage_tokens`
from the naming pattern is the obvious guess and yields a permanently empty
panel that looks exactly like "no agents have run".

WHY A SEPARATE BOARD FROM THE GOLDEN SIGNALS

Because the failure modes have almost nothing in common. A Temporal board asks
whether work is flowing: latency, error rate, saturation, queue depth. Those
questions are all answered here too — the agent Workflows appear on the existing
board like any other — but they miss the three ways an agent fleet actually goes
wrong, and all three are invisible to every panel in this repo:

  1. It loops. No error, no retry, no latency change, no backlog. Spend rises
     linearly and nothing else moves.
  2. It gets more expensive without getting slower. A prompt change, a model
     swap, or a cache that stopped hitting. Every RED signal stays flat.
  3. It quietly stops using a tool. Recall drops, answers get worse, and no
     technical signal registers anything at all.

None of those are error rates, so none of them fire an alert built on one. That
is the whole argument for this board existing.
"""
import json, sys, pathlib

OUT = sys.argv[1] if len(sys.argv) > 1 else \
    str(pathlib.Path(__file__).resolve().parent.parent /
        "demo/grafana/dashboards/custom/ai-agent-observability.json")

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

GOOD, WARN, BAD = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C4, C5 = "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"
TEMPORAL_INDIGO = "#444CE7"

# The selector every panel shares.
#
# service_name is included so the Go agent Worker and the LangChain Worker can
# be compared or combined; it defaults to All. Both emit identical series names,
# which is the entire point of the contract — see demo/app/agent_telemetry.go.
SEL = 'service_name=~"$service", gen_ai_system=~"$provider"'

# Run-, tool- and loop-scoped series carry NO gen_ai_system label. They are
# recorded once per run (or per tool call), at which point the work is not
# attributable to a single provider — an agent can switch models mid-run, and a
# refusal fallback does precisely that. Applying the provider filter to them
# would match nothing and empty half the board.
RUN_SEL = 'service_name=~"$service"'

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

def table(title, gp, targets, desc, overrides=None):
    return {"type": "table", "title": title, "id": nid(), "datasource": DS,
            "gridPos": gp, "targets": targets, "description": desc,
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                            "overrides": overrides or []},
            "options": {"showHeader": True, "cellHeight": "sm",
                        "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""}},
            "transformations": [{"id": "organize", "options": {
                "excludeByName": {"Time": True, "job": True, "instance": True,
                                  "group": True, "service_namespace": True,
                                  "deployment_environment": True}}}]}

P = []

# =========================================================================
# HERO STRIP — the four numbers a person needs before they have a question.
#
# Cost first, deliberately. On every other board in this repo the lead signal
# is availability, because that is what a platform owner is asked about. For an
# agent fleet the question that actually arrives is "why did the bill go up",
# and it arrives from someone who will never open a latency panel.
# =========================================================================
P.append({
    "type": "text", "title": "", "id": nid(), "transparent": True,
    "gridPos": {"h": 5, "w": 3, "x": 0, "y": 0},
    "options": {"mode": "html", "code": {"language": "plaintext",
                                         "showLineNumbers": False, "showMiniMap": False},
                "content": f'''<div style="background:{TEMPORAL_INDIGO};border-radius:3px;
  height:100%;display:flex;flex-direction:column;align-items:center;
  justify-content:center;padding:0.6em;text-align:center;color:#fff;">
  <div style="font-size:15px;font-weight:700;letter-spacing:.02em;">AI Agents</div>
  <div style="font-size:11px;opacity:.85;margin-top:2px;">OTel GenAI</div>
</div>'''}})

P.append(stat("Spend rate", {"h": 5, "w": 4, "x": 3, "y": 0},
    [tgt(f'sum(rate(gen_ai_client_cost_total{{{SEL}}}[$__rate_interval])) * 3600')],
    "currencyUSD",
    "Estimated USD per hour at the CURRENT rate, extrapolated from the selected window.\n\n"
    "This is a LIST-PRICE estimate and will not match an invoice: negotiated discounts, "
    "committed-use terms and batch pricing all move the real figure. Read it as a trend and "
    "an attribution tool, never as a finance source of truth.\n\n"
    "A model with no entry in agentPricing contributes ZERO here — see the 'Unpriced models' "
    "panel at the bottom, which exists to stop that silently understating the total.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None},
                                       {"color": WARN, "value": 5},
                                       {"color": BAD, "value": 20}]},
    decimals=3, novalue="no spend"))

P.append(stat("Tokens / sec", {"h": 5, "w": 4, "x": 7, "y": 0},
    [tgt(f'sum(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type=~"input|output"}}[$__rate_interval]))')],
    "short",
    "Input + output tokens per second.\n\nDeliberately EXCLUDES the reasoning and cached "
    "series: both are subsets already counted inside input/output, and summing all four "
    "double-counts. This is the one place on the board where the token_type filter is "
    "load-bearing rather than cosmetic.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=1))

P.append(stat("Agent runs / min", {"h": 5, "w": 4, "x": 11, "y": 0},
    [tgt(f'sum(rate(gen_ai_agent_runs_total{{{RUN_SEL}}}[$__rate_interval])) * 60')],
    "short",
    "Completed agent runs per minute, all outcomes.\n\nThe denominator for every per-run "
    "figure on this board.\n\n"
    "Note this panel does NOT respect the Provider picker, and cannot: a run is recorded "
    "once at the end, when it is no longer attributable to a single provider — an agent may "
    "switch models between iterations, and a refusal fallback does exactly that. Filtering "
    "run counts by provider would quietly drop those runs rather than splitting them.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=1))

P.append(stat("Cost per run", {"h": 5, "w": 4, "x": 15, "y": 0},
    [tgt(f'sum(rate(gen_ai_client_cost_total{{{SEL}}}[$__rate_interval])) / '
         f'clamp_min(sum(rate(gen_ai_agent_runs_total{{{RUN_SEL}}}[$__rate_interval])), 0.0001)')],
    "currencyUSD",
    "Mean USD per agent run.\n\nTHE MOST USEFUL SINGLE NUMBER ON THIS BOARD, because it is "
    "the one that survives a change in traffic. Total spend rising with volume is expected; "
    "cost per run rising is a regression, and it moves for reasons nothing else here shows — "
    "a longer prompt, a chattier model, a cache that stopped hitting, an agent taking more "
    "turns to reach the same answer.\n\n"
    "clamp_min avoids a divide-by-zero producing +Inf on an idle fleet.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None},
                                       {"color": WARN, "value": 0.05},
                                       {"color": BAD, "value": 0.25}]},
    decimals=4, novalue="idle"))

P.append(stat("Cache hit ratio", {"h": 5, "w": 5, "x": 19, "y": 0},
    [tgt(f'sum(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type="cached"}}[$__rate_interval])) / '
         f'clamp_min(sum(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type="input"}}[$__rate_interval])), 0.0001)')],
    "percentunit",
    "Share of input tokens served from the prompt cache.\n\nTHE SINGLE BIGGEST LEVER ON "
    "SPEND. Cached input bills at roughly a tenth of the normal rate, so a fleet that moves "
    "from 0% to 80% cuts its input cost by ~72% with no change in behaviour.\n\n"
    "It is also the metric most likely to regress silently: prompt caching is a PREFIX match, "
    "so a timestamp, a UUID or a per-user string interpolated anywhere near the front of the "
    "prompt invalidates everything after it. Nothing breaks, nothing slows down, and the bill "
    "roughly triples. If this drops after a deploy, look at what got added to the top of the "
    "system prompt.",
    thr={"mode": "absolute", "steps": [{"color": BAD, "value": None},
                                       {"color": WARN, "value": 0.2},
                                       {"color": GOOD, "value": 0.5}]},
    decimals=2, novalue="no cache"))

# =========================================================================
# ROW A — COST
# =========================================================================
P.append(row("A · Cost and token flow", 5))

P.append(ts("Spend by provider and model", {"h": 8, "w": 12, "x": 0, "y": 6},
    [tgt(f'sum by (gen_ai_system, gen_ai_response_model) '
         f'(rate(gen_ai_client_cost_total{{{SEL}}}[$__rate_interval])) * 3600',
         "{{gen_ai_system}} / {{gen_ai_response_model}}")],
    "currencyUSD",
    "USD per hour, split by provider and by the model that ACTUALLY served the request.\n\n"
    "Split on gen_ai_response_model rather than gen_ai_request_model on purpose. The two "
    "differ whenever the provider serves something other than what was asked for — a refusal "
    "fallback re-running on a second model, or an alias resolving to a dated snapshot. "
    "Attributing that spend to the requested model would hide the fallback entirely, and a "
    "fallback is exactly the event you want to see on a cost chart.",
    stack=True, fill=25, decimals=3, legend_mode="table",
    calcs=["lastNotNull", "max"]))

P.append(ts("Token flow by type", {"h": 8, "w": 12, "x": 12, "y": 6},
    [tgt(f'sum by (gen_ai_token_type) '
         f'(rate(gen_ai_client_token_usage_sum{{{SEL}}}[$__rate_interval]))',
         "{{gen_ai_token_type}}")],
    "short",
    "Tokens per second by type. NOT STACKED, deliberately.\n\n"
    "'reasoning' is a subset of 'output' and 'cached' is a subset of 'input', so stacking "
    "these four would draw a total that is roughly double the truth. They share an axis so "
    "the RATIOS are readable, which is what matters:\n\n"
    "  reasoning / output   how much you are paying for thinking you never see\n"
    "  cached / input       the cache hit ratio, in its raw form\n\n"
    "Input climbing faster than output is the signature of a long agent loop: every "
    "iteration re-sends the whole conversation, so input grows QUADRATICALLY with loop depth "
    "while output stays flat.",
    fill=0, decimals=1, legend_mode="table", calcs=["lastNotNull", "mean"]))

# =========================================================================
# ROW B — AGENT BEHAVIOUR
#
# The row that has no equivalent anywhere else in this repo.
# =========================================================================
P.append(row("B · Agent behaviour — the failures no error rate shows", 14))

P.append(ts("Loop depth (iterations per run)", {"h": 8, "w": 12, "x": 0, "y": 15},
    [tgt(f'histogram_quantile(0.50, sum by (le) '
         f'(rate(gen_ai_agent_loop_iterations_bucket{{{RUN_SEL}}}[$__rate_interval])))', "p50"),
     tgt(f'histogram_quantile(0.95, sum by (le) '
         f'(rate(gen_ai_agent_loop_iterations_bucket{{{RUN_SEL}}}[$__rate_interval])))', "p95", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le) '
         f'(rate(gen_ai_agent_loop_iterations_bucket{{{RUN_SEL}}}[$__rate_interval])))', "p99", ref="C")],
    "short",
    "Model calls made before a run reached a final answer.\n\n"
    "THE RUNAWAY DETECTOR. An agent that loops is the most expensive failure mode there is "
    "and the least visible: nothing errors, nothing retries, nothing slows down, no queue "
    "backs up. Every Temporal signal in this repo stays flat while the bill climbs.\n\n"
    "Watch the p99 rather than the mean. A fleet where most runs finish in two turns and one "
    "in a hundred grinds to the cap has a healthy-looking average and a real problem.\n\n"
    "A p99 pinned exactly at MaxIterations means runs are being CUT OFF, not finishing — "
    "cross-check against the outcome panel to the right.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None},
                                       {"color": WARN, "value": 5},
                                       {"color": BAD, "value": 8}]},
    decimals=1, calcs=["lastNotNull", "max"]))

P.append(ts("Run outcomes", {"h": 8, "w": 12, "x": 12, "y": 15},
    [tgt(f'sum by (outcome) (rate(gen_ai_agent_runs_total{{{RUN_SEL}}}[$__rate_interval])) * 60',
         "{{outcome}}")],
    "short",
    "Runs per minute by terminal outcome.\n\n"
    "  completed        the model produced a final answer\n"
    "  max_iterations   the CAP stopped it, not the model — a full-price run that "
    "answered nothing\n"
    "  error            the provider or a tool failed the run outright\n\n"
    "max_iterations is the one to watch and the one a conventional error rate misses "
    "entirely: from Temporal's point of view the Workflow COMPLETED successfully. It "
    "returned, it did not throw, and it burned the entire token budget getting nowhere.\n\n"
    "A steady trickle of these usually means the cap is too low for the task. A sudden rise "
    "usually means a tool started returning something the model cannot use, and it is "
    "retrying its way around the problem.",
    stack=True, fill=25, decimals=2, legend_mode="table", calcs=["lastNotNull"]))

P.append(ts("Tool calls by tool and outcome", {"h": 8, "w": 12, "x": 0, "y": 23},
    [tgt(f'sum by (gen_ai_tool_name, outcome) '
         f'(rate(gen_ai_agent_tool_calls_total{{{RUN_SEL}}}[$__rate_interval])) * 60',
         "{{gen_ai_tool_name}} · {{outcome}}")],
    "short",
    "Tool invocations per minute.\n\n"
    "Two outcome values are worth alerting on and neither is an error in any conventional "
    "sense:\n\n"
    "  bad_arguments   the model emitted JSON the schema rejects. Usually a tool description "
    "that is ambiguous, not a broken model — and retrying cannot fix it.\n"
    "  unknown_tool    the model asked for a tool that does not exist. This means the prompt "
    "and the tool list have DRIFTED APART, which happens quietly whenever a tool is removed "
    "and the system prompt still mentions it.\n\n"
    "The absence of a tool matters as much as its failures. A tool whose call rate drops to "
    "zero after a prompt change is a silent capability regression: answers get worse, no "
    "technical signal moves at all.",
    fill=0, decimals=2, legend_mode="table", calcs=["lastNotNull"]))

P.append(ts("Reasoning token share", {"h": 8, "w": 12, "x": 12, "y": 23},
    [tgt(f'sum by (gen_ai_response_model) '
         f'(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type="reasoning"}}[$__rate_interval])) / '
         f'clamp_min(sum by (gen_ai_response_model) '
         f'(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type="output"}}[$__rate_interval])), 0.0001)',
         "{{gen_ai_response_model}}")],
    "percentunit",
    "Fraction of billed output tokens spent on internal reasoning.\n\n"
    "Reasoning tokens are billed at the output rate but never appear in the response body. "
    "Without this panel a bill that grows while visible output stays constant has no "
    "explanation anywhere in your telemetry.\n\n"
    "This is the panel that tells you whether an effort setting is earning its cost. Moving "
    "from 'high' to 'xhigh' can double this share; whether the answers got better is a "
    "question only your evals can answer, but this is what tells you what you PAID for the "
    "attempt.\n\n"
    "Empty is normal and correct for a model with thinking disabled, or for the mock provider.",
    fill=0, decimals=3, minv=0, maxv=1, legend_mode="table", calcs=["lastNotNull", "mean"]))

# =========================================================================
# ROW C — MODEL CALL HEALTH
# =========================================================================
P.append(row("C · Model call health", 31))

P.append(ts("Model call latency", {"h": 8, "w": 12, "x": 0, "y": 32},
    [tgt(f'histogram_quantile(0.50, sum by (le, gen_ai_system) '
         f'(rate(gen_ai_client_operation_duration_seconds_bucket{{{SEL}}}[$__rate_interval])))',
         "p50 {{gen_ai_system}}"),
     tgt(f'histogram_quantile(0.95, sum by (le, gen_ai_system) '
         f'(rate(gen_ai_client_operation_duration_seconds_bucket{{{SEL}}}[$__rate_interval])))',
         "p95 {{gen_ai_system}}", ref="B"),
     tgt(f'histogram_quantile(0.99, sum by (le, gen_ai_system) '
         f'(rate(gen_ai_client_operation_duration_seconds_bucket{{{SEL}}}[$__rate_interval])))',
         "p99 {{gen_ai_system}}", ref="C")],
    "s",
    "Wall-clock duration of ONE model call, bracketing the HTTP request only.\n\n"
    "It deliberately excludes Activity scheduling. Folding Temporal queue latency in here "
    "would produce a panel labelled 'model latency' that rises when your OWN Worker fleet is "
    "backed up — blaming the provider for your capacity problem. To see that component, "
    "compare against activity schedule-to-start on the golden-signals board; the gap between "
    "them IS your queueing.\n\n"
    "Buckets run to 300s because a high-effort call on a hard prompt genuinely takes minutes. "
    "The OTel SDK's default boundaries stop at 10s, which would put every interesting call in "
    "+Inf and make every quantile here a lower bound rather than a measurement.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None},
                                       {"color": WARN, "value": 20},
                                       {"color": BAD, "value": 60}]},
    decimals=2, legend_mode="table", calcs=["lastNotNull", "max"]))

P.append(ts("Model call errors by type", {"h": 8, "w": 12, "x": 12, "y": 32},
    [tgt(f'sum by (error_type, gen_ai_system) '
         f'(rate(gen_ai_client_operation_duration_seconds_count{{{SEL}, error_type!=""}}[$__rate_interval])) * 60',
         "{{gen_ai_system}} · {{error_type}}")],
    "short",
    "Failed model calls per minute, bucketed by a taxonomy chosen for WHAT YOU DO NEXT:\n\n"
    "  rate_limit    back off, or raise the account limit\n"
    "  overloaded    provider-side; retry, nothing to fix locally\n"
    "  auth          a human must rotate a key — retrying is pure waste\n"
    "  timeout       raise the Activity timeout, or lower effort\n"
    "  refusal       a content decision, NOT a fault; never retry the same prompt\n"
    "  bad_request   a malformed call, i.e. a code bug; also never retryable\n\n"
    "The last three are marked NonRetryable in the Workflow's retry policy, so the same "
    "taxonomy drives both this panel and the retry decision. That is deliberate: a bucket "
    "added to one and not the other means paying three times for an error that was never "
    "going to succeed.\n\n"
    "error_type is present ONLY on failures. Successful calls carry no such label, which is "
    "why this query filters on error_type!=\"\" rather than on a status value.",
    thr={"mode": "absolute", "steps": [{"color": C3, "value": None}, {"color": BAD, "value": 1}]},
    fill=25, stack=True, decimals=2, legend_mode="table", calcs=["lastNotNull"]))

# =========================================================================
# ROW D — COST HYGIENE
#
# Panels about whether the OTHER panels can be trusted. An observability system
# that cannot detect its own blind spots invites more confidence than it earns.
# =========================================================================
P.append(row("D · Cost hygiene — can the numbers above be trusted?", 40))

P.append(table("Unpriced models (spend is UNDER-REPORTED)", {"h": 7, "w": 12, "x": 0, "y": 41},
    [tgt(f'sum by (gen_ai_system, gen_ai_response_model) '
         f'(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type="output"}}[$__rate_interval])) '
         f'unless '
         f'sum by (gen_ai_system, gen_ai_response_model) '
         f'(rate(gen_ai_client_cost_total{{{SEL}}}[$__rate_interval])) > 0',
         instant=True)],
    "Models that are burning output tokens but contributing NOTHING to the cost panels.\n\n"
    "The pricing table is a hand-maintained list of published rates. A model missing from it "
    "contributes zero rather than a guess — a confident wrong number on a cost dashboard is "
    "worse than a visible gap, because someone will budget against it.\n\n"
    "This panel is that visible gap. EMPTY IS THE HEALTHY STATE. Any row here means every "
    "cost figure on this board is an understatement, and the fix is to add the model's list "
    "price to agentPricing in demo/app/agent_types.go AND to PRICING in "
    "demo/agent-python/telemetry.py — both, or the two workers will disagree, which is what "
    "demo/agent-python/test_parity.py exists to catch.\n\n"
    "The `unless` operator does the work: token series that have no matching cost series."))

P.append(ts("Input tokens per model call", {"h": 7, "w": 12, "x": 12, "y": 41},
    [tgt(f'sum(rate(gen_ai_client_token_usage_sum{{{SEL}, gen_ai_token_type="input"}}[$__rate_interval])) / '
         f'clamp_min(sum(rate(gen_ai_client_operation_duration_seconds_count{{{SEL}, error_type=""}}[$__rate_interval])), 0.0001)',
         "mean input tokens / call")],
    "short",
    "Mean input tokens per successful model call — a proxy for prompt size.\n\n"
    "It is the leading indicator for the cost-per-run panel at the top, and it moves for "
    "reasons that are easy to ship without noticing: a system prompt that grew, a tool schema "
    "that got more verbose, retrieved context that stopped being truncated, or an agent "
    "taking more turns (each turn re-sends the entire conversation).\n\n"
    "A step change here immediately after a deploy is the cheapest cost regression you will "
    "ever catch, because the cause is in the diff.",
    thr={"mode": "absolute", "steps": [{"color": C1, "value": None}]},
    decimals=0, calcs=["lastNotNull", "max"]))

# =========================================================================
dash = {
    "annotations": {"list": []},
    "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1,
    "links": [
        {"asDropdown": False, "icon": "dashboard", "tags": [], "targetBlank": False,
         "title": "Golden signals", "tooltip": "Temporal RED + saturation for these Workers",
         "type": "link", "url": "/d/temporal-golden-signals"},
        {"asDropdown": False, "icon": "dashboard", "tags": [], "targetBlank": False,
         "title": "Worker scale", "tooltip": "Capacity and slot sizing for the agent fleet",
         "type": "link", "url": "/d/temporal-worker-scale"},
    ],
    "panels": P, "preload": False,
    "refresh": "30s", "schemaVersion": 39,
    "tags": ["temporal", "genai", "agents", "cost"],
    "templating": {"list": [
        {"current": {}, "hide": 0, "includeAll": False, "label": "Data source",
         "multi": False, "name": "DS_PROMETHEUS", "options": [], "query": "prometheus",
         "refresh": 1, "regex": "", "skipUrlSync": False, "type": "datasource"},
        # Both variables are driven off a series that only exists once an agent
        # has run. Before the first run the pickers are empty and every panel
        # reads "No data" — which is correct, and is why the docs tell you to
        # run `make agent-demo` before opening this board.
        {"current": {"selected": True, "text": ["All"], "value": ["$__all"]},
         "datasource": DS,
         "definition": "label_values(gen_ai_client_token_usage_sum, service_name)",
         "hide": 0, "includeAll": True, "allValue": ".*", "label": "Worker",
         "multi": True, "name": "service", "options": [],
         "query": {"qryType": 1,
                   "query": "label_values(gen_ai_client_token_usage_sum, service_name)",
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
        {"current": {"selected": True, "text": ["All"], "value": ["$__all"]},
         "datasource": DS,
         "definition": "label_values(gen_ai_client_token_usage_sum, gen_ai_system)",
         "hide": 0, "includeAll": True, "allValue": ".*", "label": "Provider",
         "multi": True, "name": "provider", "options": [],
         "query": {"qryType": 1,
                   "query": "label_values(gen_ai_client_token_usage_sum, gen_ai_system)",
                   "refId": "PrometheusVariableQueryEditor-VariableQuery"},
         "refresh": 2, "regex": "", "skipUrlSync": False, "sort": 1, "type": "query"},
    ]},
    "time": {"from": "now-1h", "to": "now"}, "timepicker": {}, "timezone": "browser",
    "title": "AI Agents — GenAI Observability",
    "uid": "ai-agent-observability",
    "version": 1, "weekStart": "",
}

with open(OUT, "w") as f:
    json.dump(dash, f, indent=2)
    f.write("\n")
print(f"wrote {OUT}")
print(f"  {len([p for p in P if p['type']!='row'])} panels, {len([p for p in P if p['type']=='row'])} rows")
