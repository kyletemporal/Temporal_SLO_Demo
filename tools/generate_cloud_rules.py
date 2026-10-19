#!/usr/bin/env python3
"""Generate the Temporal CLOUD SLO rule file."""
import pathlib

from promql_guards import outcome_ratio_terms

import pathlib
OUT = str(pathlib.Path(__file__).resolve().parent.parent / "cloud/prometheus/slo-rules.yml")

WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d", "28d"]
BURN_WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d"]
COMPLIANCE = "28d"

# Minimum request rate before a percentile-based SLI is trusted. Temporal's own
# docs warn that percentiles are computed per 1-minute window, so a namespace
# doing a handful of requests per minute has p50/p95/p99 all converging on the
# single slowest request.
MIN_RPS = "0.33"   # ~20 requests/minute, the threshold the docs cite

# Every SLI is aggregated per Namespace EXPLICITLY. The metrics carry
# different label sets — request/error counts have `operation`, activity
# timeouts add `timeout_type`, task metrics add `temporal_task_queue` and
# `task_type` — and a bare `+` or `/` between them matches nothing and returns
# EMPTY, not a wrong number. task_delivery and activity_completion both shipped
# that way. Summing to `temporal_namespace` first makes every pair line up.
# Labels per https://docs.temporal.io/cloud/metrics/openmetrics/metrics-reference
NS = "sum by (temporal_namespace) ({})"
AVG = "sum by (temporal_namespace) (avg_over_time({}[5m]))"

_wf_bad, _wf_total = outcome_ratio_terms(
    {"success": "temporal_cloud_v1_workflow_success_count",
     "failed": "temporal_cloud_v1_workflow_failed_count",
     "timeout": "temporal_cloud_v1_workflow_timeout_count"},
    bad=("failed", "timeout"), wrap=AVG)
_act_bad, _act_total = outcome_ratio_terms(
    {"success": "temporal_cloud_v1_activity_success_count",
     "fail": "temporal_cloud_v1_activity_fail_count",
     "timeout": "temporal_cloud_v1_activity_timeout_count"},
    bad=("fail", "timeout"), wrap=AVG)

SLIS = [
    # ---------------------------------------------------------------------
    # Mirrors the SLA's own formula: 1 - (errors / requests) per Namespace.
    # Errors are a subset of requests, so guarding on requests is safe here.
    # ---------------------------------------------------------------------
    dict(name="cloud_service_availability", objective=0.999,
         desc="gRPC requests to the Namespace that did not return a service error",
         bad=f'{AVG.format("temporal_cloud_v1_service_error_count")}\n'
             f'            or {AVG.format("temporal_cloud_v1_service_request_count")} * 0',
         total=AVG.format("temporal_cloud_v1_service_request_count"),
         by="temporal_namespace"),

    # ---------------------------------------------------------------------
    # Latency uses the PRE-CALCULATED percentile. There is no histogram and no
    # bucket, so this is not a good/total ratio — it is "was p95 under target".
    # Expressed as a bad-event ratio of 0 or 1 so it flows through the same
    # budget machinery, gated on having enough traffic to mean anything.
    # The percentile itself is never aggregated; only the 0/1 verdicts are.
    # ---------------------------------------------------------------------
    dict(name="cloud_start_workflow_latency", objective=0.99,
         desc="Minutes where StartWorkflowExecution p95 stayed under 500ms",
         bad=NS.format(
             '\n            (temporal_cloud_v1_service_latency_p95{operation="StartWorkflowExecution"} > bool 0.5)\n'
             f'            and (temporal_cloud_v1_service_request_count{{operation="StartWorkflowExecution"}} > {MIN_RPS})\n'
             '          '),
         total=NS.format(f'temporal_cloud_v1_service_request_count{{operation="StartWorkflowExecution"}} > bool {MIN_RPS}'),
         by="temporal_namespace"),

    # ---------------------------------------------------------------------
    # Application outcome. Timeouts count as bad; cancellations do not —
    # a cancellation is somebody getting what they asked for. Guarded against
    # the union of outcomes (tools/promql_guards.py): the old success-based
    # guard went EMPTY in a total outage.
    # ---------------------------------------------------------------------
    dict(name="workflow_completion", objective=0.99,
         desc="Workflow Executions completing successfully; timeouts count as bad",
         bad=_wf_bad, total=_wf_total, by="temporal_namespace"),

    dict(name="activity_completion", objective=0.99,
         desc="Activity Executions completing successfully; timeouts count as bad",
         bad=_act_bad, total=_act_total, by="temporal_namespace"),

    # ---------------------------------------------------------------------
    # Task delivery. no_poller_tasks is the rate of Tasks added to a queue
    # with nobody listening — on Cloud this is YOUR fault, not Temporal's,
    # because you run the Workers.
    #
    # Denominator: Tasks matched to a poller (poll_success) plus Tasks that
    # found no poller — both are TASKS. The previous denominator added
    # Workflow completions to Tasks (different units AND different labels, so
    # it matched nothing and this SLI was always empty). Approximate: a
    # no-poller Task can be matched later and then counts on both sides.
    # ---------------------------------------------------------------------
    dict(name="task_delivery", objective=0.999,
         desc="Tasks arriving on a Task Queue that had an active poller",
         bad=f'{AVG.format("temporal_cloud_v1_no_poller_tasks_count")}\n'
             f'            or {AVG.format("temporal_cloud_v1_poll_success_count")} * 0',
         total='(\n'
               f'            {AVG.format("temporal_cloud_v1_poll_success_count")}\n'
               '            +\n'
               f'            ({AVG.format("temporal_cloud_v1_no_poller_tasks_count")} or {AVG.format("temporal_cloud_v1_poll_success_count")} * 0)\n'
               '          )',
         by="temporal_namespace"),
]

HEADER = f'''# =============================================================================
# Temporal CLOUD — SLOs, error budgets and burn-rate alerts
# =============================================================================
#
# Compliance window: {COMPLIANCE}. Per-Namespace.
#
# ---------------------------------------------------------------------------
# READ THIS FIRST: Cloud metrics are NOT self-hosted metrics
# ---------------------------------------------------------------------------
# Three differences make most self-hosted PromQL wrong here.
#
# 1. RATES ARE PRE-COMPUTED. Every `_count` metric from the OpenMetrics
#    endpoint is a GAUGE holding a per-second rate, already aggregated over a
#    1-minute window. Do NOT wrap them in rate() or increase(). rate() of a
#    gauge is meaningless and will silently produce near-zero numbers that look
#    plausible. Use avg_over_time() to widen the window, as this file does.
#
# 2. PERCENTILES ARE PRE-CALCULATED AND CANNOT BE RE-AGGREGATED. There are no
#    histogram buckets, so histogram_quantile() does not apply. Temporal's docs
#    are explicit that aggregating `_p95` across dimensions produces a wrong
#    number. Nor can you widen a 1-minute p95 into an accurate 1-hour p95.
#
# 3. THERE ARE NO SERVER INTERNALS. You do not run frontend, history, matching
#    or the datastore, so there is no persistence_latency, no shard metrics and
#    no service_name label. What you get is the Namespace's external behaviour
#    plus whatever your own Workers emit.
#
# ---------------------------------------------------------------------------
# RECORD NAMES ARE PREFIXED `cloudslo:` ON PURPOSE
# ---------------------------------------------------------------------------
# The self-hosted bundle in this repo records `slo:*`. A team running BOTH a
# self-hosted cluster and Temporal Cloud into one Prometheus would otherwise
# get two rule sets writing the same series names with overlapping `sli`
# labels — silently merging Cloud and self-hosted SLIs into one number. The
# prefix keeps them separate.
#
# ---------------------------------------------------------------------------
# Your availability SLI will NOT equal Temporal's SLA calculation
# ---------------------------------------------------------------------------
# The SLA excludes a specific list of error types (NotFound, InvalidArgument,
# PermissionDenied, Throttling, WorkflowExecutionAlreadyStarted, and others -
# see docs/SLA-AND-SLOS.md). But `temporal_cloud_v1_service_error_count` is
# labelled only by `operation`. There is NO error-type dimension, so those
# exclusions CANNOT be applied to the metric.
#
# Consequence: the availability SLI below is a CONSERVATIVE SUPERSET. It counts
# errors the SLA forgives, so it will read lower than Temporal's own figure.
# That is fine for running your service — you want to know about caller errors
# too — but do not use this number to argue a service credit. Temporal's
# calculation is authoritative for the SLA.
#
# ---------------------------------------------------------------------------
# Do not promise more than Temporal promises you
# ---------------------------------------------------------------------------
# The contractual SLA is 99.9% on a standard single-region Namespace, and
# 99.99% on a High Availability Namespace. Any availability SLO you offer your
# own customers inherits that ceiling for everything Temporal owns. Promising
# 99.99% on a standard Namespace is promising something you have not bought.
#
# The default objective below is 99.9% to match the standard SLA. On an HA
# Namespace you may raise it to 99.99%.
# =============================================================================

groups:
'''


def emit():
    out = [HEADER]
    out.append("  # ==========================================================================\n")
    out.append("  # LAYER 1 — event rates. Two rules per SLI.\n")
    out.append("  # ==========================================================================\n")
    out.append("  - name: temporal-cloud-slo-events\n    interval: 60s\n    rules:\n")
    for s in SLIS:
        out.append(f"\n      # --- {s['name']}: {s['desc']}\n")
        out.append("      - record: cloudslo:events_bad:rate5m\n        expr: |\n")
        out.append(f"          {s['bad']}\n        labels:\n          sli: {s['name']}\n")
        out.append("      - record: cloudslo:events_total:rate5m\n        expr: |\n")
        out.append(f"          {s['total']}\n        labels:\n          sli: {s['name']}\n")

    out.append("\n  # ==========================================================================\n")
    out.append("  # LAYER 2 — ratios, objectives, budgets, burn rates. Generic.\n")
    out.append("  # ==========================================================================\n")
    out.append("  - name: temporal-cloud-slo-meta\n    interval: 60s\n    rules:\n")
    out.append("\n      # avg_over_time over the 5m recordings. Correct AND cheap: because\n")
    out.append("      # avg(rate) == events / window, dividing avg_bad by avg_total gives the\n")
    out.append("      # traffic-weighted ratio rather than an average of ratios.\n")
    for w in WINDOWS:
        out.append(f"      - record: cloudslo:sli_bad:ratio_rate{w}\n        expr: |\n")
        if w == "5m":
            out.append("          cloudslo:events_bad:rate5m\n          /\n          cloudslo:events_total:rate5m\n")
        else:
            out.append(f"          avg_over_time(cloudslo:events_bad:rate5m[{w}])\n")
            out.append(f"          /\n          avg_over_time(cloudslo:events_total:rate5m[{w}])\n")

    out.append("\n      # Objectives. 99.9% matches the standard single-region SLA.\n")
    out.append("      # Raise cloud_service_availability to 0.9999 on an HA Namespace.\n")
    for s in SLIS:
        out.append(f"      - record: cloudslo:objective:ratio\n        expr: vector({s['objective']})\n")
        out.append(f"        labels:\n          sli: {s['name']}\n")

    out.append("\n      - record: cloudslo:error_budget:ratio\n        expr: 1 - cloudslo:objective:ratio\n")
    out.append("\n      - record: cloudslo:compliance_window_bad:ratio\n")
    out.append(f"        expr: cloudslo:sli_bad:ratio_rate{COMPLIANCE}\n")

    for w in BURN_WINDOWS:
        out.append(f"      - record: cloudslo:burn_rate:ratio_rate{w}\n        expr: |\n")
        out.append(f"          cloudslo:sli_bad:ratio_rate{w}\n            / on(sli) group_left() cloudslo:error_budget:ratio\n")

    out.append("\n      - record: cloudslo:error_budget_remaining:ratio\n        expr: |\n")
    out.append("          1 - (\n            cloudslo:compliance_window_bad:ratio\n")
    out.append("              / on(sli) group_left() cloudslo:error_budget:ratio\n          )\n")
    out.append("\n      - record: cloudslo:sli_good:ratio\n        expr: 1 - cloudslo:compliance_window_bad:ratio\n")
    out.append("\n      - record: cloudslo:objective_expanded:ratio\n        expr: |\n")
    out.append("          cloudslo:compliance_window_bad:ratio * 0\n            + on(sli) group_left() cloudslo:objective:ratio\n")

    out.append(f'''
  # ==========================================================================
  # LAYER 3 — burn-rate alerts. Identical model to the self-hosted bundle.
  #
  # ALERT NAMES ARE PREFIXED `CloudSLO` for the same reason the records are
  # prefixed `cloudslo:`. A team running self-hosted AND Cloud in one
  # Prometheus would otherwise see two different alerts both called
  # SLOFastBurn, and an on-call has no way to tell which stack fired.
  # ==========================================================================
  - name: temporal-cloud-slo-burn
    rules:
      - alert: CloudSLOFastBurn
        expr: |
          (cloudslo:burn_rate:ratio_rate1h > 14.4) and (cloudslo:burn_rate:ratio_rate5m > 14.4)
        for: 2m
        labels: {{severity: page, component: temporal-cloud-slo}}
        annotations:
          summary: "{{{{ $labels.sli }}}} burning budget 14.4x in {{{{ $labels.temporal_namespace }}}}"
          description: "The entire {COMPLIANCE} budget goes in about two days at this rate (14.4x a 28-day budget lasts ~47 hours). If cloud_service_availability is the SLI, check https://status.temporal.io before assuming it is yours."

      - alert: CloudSLOSlowBurn
        expr: |
          (cloudslo:burn_rate:ratio_rate6h > 6) and (cloudslo:burn_rate:ratio_rate30m > 6)
        for: 15m
        labels: {{severity: page, component: temporal-cloud-slo}}
        annotations:
          summary: "{{{{ $labels.sli }}}} burning budget 6x in {{{{ $labels.temporal_namespace }}}}"
          description: "Slower, still exhausts the budget well before the window ends."

      - alert: CloudSLOBudgetBurnTicket
        expr: |
          (cloudslo:burn_rate:ratio_rate1d > 3) and (cloudslo:burn_rate:ratio_rate2h > 3)
        for: 1h
        labels: {{severity: ticket, component: temporal-cloud-slo}}
        annotations:
          summary: "{{{{ $labels.sli }}}} burning budget 3x in {{{{ $labels.temporal_namespace }}}}"
          description: "Not an emergency. Worth a ticket before it becomes one."

      - alert: CloudSLOErrorBudgetExhausted
        expr: cloudslo:error_budget_remaining:ratio <= 0
        for: 10m
        labels: {{severity: ticket, component: temporal-cloud-slo}}
        annotations:
          summary: "{{{{ $labels.sli }}}} exhausted its {COMPLIANCE} budget in {{{{ $labels.temporal_namespace }}}}"
          description: "If this is cloud_service_availability and Temporal was at fault, you may have an SLA claim - but use Temporal's own figure, not this one. See docs/SLA-AND-SLOS.md."
''')

    pathlib.Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        f.write("".join(out))
    print(f"wrote {OUT}")
    print(f"  {len(SLIS)} SLIs x 2 = {len(SLIS)*2} layer-1 rules")


emit()
