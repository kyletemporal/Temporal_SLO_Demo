#!/usr/bin/env python3
"""Generate the production SLO rule file."""
import pathlib

OUT = str(pathlib.Path(__file__).resolve().parent.parent / "production/prometheus/slo-rules.yml")

# Alert windows for multi-window multi-burn-rate, plus the compliance window.
WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d", "28d"]
BURN_WINDOWS = ["5m", "30m", "1h", "2h", "6h", "1d", "3d"]
COMPLIANCE = "28d"

# Internal namespaces. Temporal's own housekeeping runs here; a tenant SLO that
# includes them is measuring your cluster's internals as if they were customer
# traffic.
TENANT = 'namespace!~"temporal_system|system|_unknown_"'

# Errors caused by the CALLER. Counting these against your availability makes
# the SLO measure your customers' behaviour rather than your service. Measured
# on a healthy idle cluster, matching alone emits ~0.39/s of Canceled on
# AddWorkflowTask/AddActivityTask, which is enough to blow a 99.9% budget many
# times over while nothing is wrong.
CLIENT_FAULTS = ('error_type!~"serviceerror_(Canceled|NotFound|NamespaceNotFound'
                 '|AlreadyExist.*|InvalidArgument|FailedPrecondition'
                 '|WorkflowExecutionAlreadyStarted|QueryFailed)"')

# Long-poll operations block for up to 60s BY DESIGN and are the highest-volume
# operations on the Frontend. Including them in a latency SLI measures how long
# Workers idle, not how fast you are. Note that Poll.* is not sufficient:
# GetTaskQueueUserData and ListNexusEndpoints are long-poll watches too.
# RE-DERIVE THIS LIST ON YOUR CLUSTER — see docs/SLO-GUIDE.md.
NOT_LONG_POLL = 'operation!~"Poll.*|GetTaskQueueUserData|ListNexusEndpoints"'

LAT_LE = "0.5"   # latency SLI objective boundary, seconds
S2S_LE = "0.2"   # task delivery boundary, seconds


def err_sli(svc, by_ns):
    by = "by (namespace) " if by_ns else ""
    ns = f", {TENANT}" if by_ns else ""
    bad = (f'(\n            sum {by}(rate(service_error_with_type{{service_name="{svc}", {CLIENT_FAULTS}{ns}}}[5m]))\n'
           f'            or\n'
           f'            sum {by}(rate(service_requests{{service_name="{svc}"{ns}}}[5m])) * 0\n'
           f'          )')
    total = f'sum {by}(rate(service_requests{{service_name="{svc}"{ns}}}[5m]))'
    return bad, total


def lat_sli(svc, by_ns):
    by = "by (namespace) " if by_ns else ""
    ns = f", {TENANT}" if by_ns else ""
    total = f'sum {by}(rate(service_latency_count{{service_name="{svc}", {NOT_LONG_POLL}{ns}}}[5m]))'
    fast = f'sum {by}(rate(service_latency_bucket{{service_name="{svc}", {NOT_LONG_POLL}, le="{LAT_LE}"{ns}}}[5m]))'
    # clamp_min guards float drift making bad marginally negative.
    bad = f'clamp_min(\n            {total}\n            -\n            {fast},\n            0\n          )'
    return bad, total


SLIS = [
    dict(name="frontend_availability", objective=0.999, tier="tenant",
         desc="Frontend gRPC requests that did not fault, per namespace",
         sli=err_sli("frontend", True)),
    dict(name="frontend_latency", objective=0.99, tier="tenant",
         desc=f"Frontend requests served under {LAT_LE}s (long-polls excluded)",
         sli=lat_sli("frontend", True)),
    dict(name="workflow_completion", objective=0.99, tier="tenant",
         desc="Workflows reaching successful completion; timeouts count as bad",
         sli=(
             '(\n'
             f'            (sum by (namespace) (rate(workflow_failed{{{TENANT}}}[5m]))  or sum by (namespace) (rate(workflow_success{{{TENANT}}}[5m])) * 0)\n'
             f'            +\n'
             f'            (sum by (namespace) (rate(workflow_timeout{{{TENANT}}}[5m])) or sum by (namespace) (rate(workflow_success{{{TENANT}}}[5m])) * 0)\n'
             '          )',
             '(\n'
             f'            sum by (namespace) (rate(workflow_success{{{TENANT}}}[5m]))\n'
             f'            +\n'
             f'            (sum by (namespace) (rate(workflow_failed{{{TENANT}}}[5m]))  or sum by (namespace) (rate(workflow_success{{{TENANT}}}[5m])) * 0)\n'
             f'            +\n'
             f'            (sum by (namespace) (rate(workflow_timeout{{{TENANT}}}[5m])) or sum by (namespace) (rate(workflow_success{{{TENANT}}}[5m])) * 0)\n'
             '          )')),
    dict(name="task_delivery", objective=0.99, tier="tenant",
         desc=f"Activity Tasks started within {S2S_LE}s of scheduling",
         sli=(
             'clamp_min(\n'
             f'            sum by (namespace) (rate(temporal_activity_schedule_to_start_latency_seconds_count{{{TENANT}}}[5m]))\n'
             '            -\n'
             f'            sum by (namespace) (rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{le="{S2S_LE}", {TENANT}}}[5m])),\n'
             '            0\n'
             '          )',
             f'sum by (namespace) (rate(temporal_activity_schedule_to_start_latency_seconds_count{{{TENANT}}}[5m]))')),

    # Infrastructure tier — shared across all tenants, so NOT per-namespace.
    # These answer "is the platform healthy", which is your question, not your
    # customer's.
    dict(name="history_availability", objective=0.999, tier="infra",
         desc="History service requests that did not fault (cluster-wide)",
         sli=err_sli("history", False)),
    dict(name="matching_availability", objective=0.999, tier="infra",
         desc="Matching service requests that did not fault (cluster-wide)",
         sli=err_sli("matching", False)),
    dict(name="persistence_availability", objective=0.999, tier="infra",
         desc="Datastore operations that did not fault, per service role",
         sli=(
             f'(\n'
             f'            sum by (service_name) (rate(persistence_error_with_type{{{CLIENT_FAULTS}}}[5m]))\n'
             f'            or\n'
             f'            sum by (service_name) (rate(persistence_requests[5m])) * 0\n'
             f'          )',
             'sum by (service_name) (rate(persistence_requests[5m]))')),
]

HEADER = f'''# =============================================================================
# Temporal SLOs, error budgets and burn-rate alerts — PRODUCTION
# =============================================================================
#
# Compliance window: {COMPLIANCE}.  Multi-tenant: tenant SLIs are per-namespace.
#
# ---------------------------------------------------------------------------
# Architecture: rates first, ratios second
# ---------------------------------------------------------------------------
# Layer 1 records the NUMERATOR and DENOMINATOR as 5m event rates, not the
# ratio. Every longer window is then derived with avg_over_time() over those
# two recordings.
#
# This matters at production scale. Computing rate(...[28d]) directly over raw
# counters makes Prometheus read 28 days of samples on every evaluation, for
# every SLI, every 30 seconds — the single most common way an SLO rule set
# takes down the monitoring system it was meant to protect. avg_over_time over
# a 5m recording reads pre-aggregated points instead.
#
# It is also arithmetically correct rather than approximate: because
# avg(rate) == total_events / window, dividing avg_bad by avg_total yields the
# traffic-WEIGHTED ratio. Averaging the per-5m ratios instead would weight a
# quiet Sunday equally with a Monday peak, which is the standard subtle bug in
# hand-rolled SLO rules.
#
#   slo:events_bad:rate5m       bad events/sec        (layer 1, per SLI)
#   slo:events_total:rate5m     total events/sec      (layer 1, per SLI)
#   slo:sli_bad:ratio_rate<W>   bad ratio over W      (layer 2, generic)
#   slo:objective:ratio         the promise           (layer 2)
#   slo:error_budget:ratio      1 - objective         (layer 2)
#   slo:burn_rate:ratio_rate<W> ratio / budget        (layer 2, generic)
#   slo:error_budget_remaining:ratio                  (layer 2)
#
# Layers 2 and 3 are written ONCE and apply to every SLI by matching on the
# `sli` label. Adding an SLI means adding two layer-1 rules; budgets, burn
# rates and alerts pick it up automatically.
#
# ---------------------------------------------------------------------------
# Before you deploy this
# ---------------------------------------------------------------------------
# 1. OBJECTIVES BELOW ARE PLACEHOLDERS. 99.9% and 99% are round numbers, not
#    measurements. Run these rules in recording-only mode for two weeks, look
#    at what your cluster actually delivers, then set objectives you can meet.
#    An SLO you miss at baseline trains everyone to ignore the board.
# 2. Prometheus needs retention >= {COMPLIANCE} AND durable storage. An error budget
#    that resets when the monitoring stack restarts will report perfect
#    attainment through an outage it has forgotten.
# 3. Re-derive the long-poll operation list on YOUR server version.
# 4. If your Workers are serverless/ephemeral, task_delivery and every
#    absence-based alert need rethinking — see docs/SERVERLESS-WORKERS.md.
# =============================================================================

groups:
'''


def emit():
    out = [HEADER]

    # ---------------- Layer 1 ----------------
    out.append("  # ==========================================================================\n")
    out.append("  # LAYER 1 — event rates. Two rules per SLI. The only per-SLI code.\n")
    out.append("  # ==========================================================================\n")
    out.append("  - name: temporal-slo-events\n    interval: 30s\n    rules:\n")
    for s in SLIS:
        bad, total = s["sli"]
        out.append(f"\n      # --- {s['name']} [{s['tier']}]: {s['desc']}\n")
        out.append("      - record: slo:events_bad:rate5m\n        expr: |\n")
        out.append(f"          {bad}\n        labels:\n          sli: {s['name']}\n          tier: {s['tier']}\n")
        out.append("      - record: slo:events_total:rate5m\n        expr: |\n")
        out.append(f"          {total}\n        labels:\n          sli: {s['name']}\n          tier: {s['tier']}\n")

    # ---------------- Layer 2 ----------------
    out.append("\n  # ==========================================================================\n")
    out.append("  # LAYER 2 — ratios, objectives, budgets, burn rates. Generic.\n")
    out.append("  # ==========================================================================\n")
    out.append("  - name: temporal-slo-meta\n    interval: 30s\n    rules:\n")

    out.append("\n      # Bad-event ratio per window. avg_over_time over the 5m recordings,\n")
    out.append("      # NOT rate() over raw counters — see the header for why.\n")
    for w in WINDOWS:
        out.append(f"      - record: slo:sli_bad:ratio_rate{w}\n        expr: |\n")
        if w == "5m":
            out.append("          slo:events_bad:rate5m\n          /\n          slo:events_total:rate5m\n")
        else:
            out.append(f"          avg_over_time(slo:events_bad:rate5m[{w}])\n")
            out.append(f"          /\n          avg_over_time(slo:events_total:rate5m[{w}])\n")

    out.append("\n      # Objectives. PLACEHOLDERS — derive from your own baseline.\n")
    for s in SLIS:
        out.append(f"      - record: slo:objective:ratio\n        expr: vector({s['objective']})\n")
        out.append(f"        labels:\n          sli: {s['name']}\n")

    out.append("\n      - record: slo:error_budget:ratio\n        expr: 1 - slo:objective:ratio\n")

    out.append("\n      # The compliance window, in one place. Repoint to change it.\n")
    out.append(f"      - record: slo:compliance_window_bad:ratio\n        expr: slo:sli_bad:ratio_rate{COMPLIANCE}\n")

    out.append("\n      # Burn rate: multiples of sustainable spend. 1.0 exhausts the budget\n")
    out.append("      # exactly at the end of the window. group_left() because SLIs carry\n")
    out.append("      # namespace/service_name labels the objective series does not.\n")
    for w in BURN_WINDOWS:
        out.append(f"      - record: slo:burn_rate:ratio_rate{w}\n        expr: |\n")
        out.append(f"          slo:sli_bad:ratio_rate{w}\n            / on(sli) group_left() slo:error_budget:ratio\n")

    out.append("\n      - record: slo:error_budget_remaining:ratio\n        expr: |\n")
    out.append("          1 - (\n            slo:compliance_window_bad:ratio\n")
    out.append("              / on(sli) group_left() slo:error_budget:ratio\n          )\n")

    out.append("\n      - record: slo:sli_good:ratio\n        expr: 1 - slo:compliance_window_bad:ratio\n")

    out.append("\n      # Objective broadcast onto each SLI's full label set, so a dashboard\n")
    out.append("      # table can join it against the others without splitting rows.\n")
    out.append("      - record: slo:objective_expanded:ratio\n        expr: |\n")
    out.append("          slo:compliance_window_bad:ratio * 0\n            + on(sli) group_left() slo:objective:ratio\n")

    # ---------------- Layer 3 ----------------
    out.append(f'''
  # ==========================================================================
  # LAYER 3 — multi-window multi-burn-rate alerts (Google SRE workbook)
  #
  # Two windows per alert, both breaching. The long window proves the burn is
  # real; the short window proves it is STILL happening, so the alert clears
  # promptly instead of lingering for the length of the long window.
  #
  # Against a {COMPLIANCE} budget:
  #   14.4x over 1h  -> 2% of the budget gone.   Page.
  #   6x    over 6h  -> 5% gone.                 Page.
  #   3x    over 1d  -> 10% gone.                Ticket.
  #   1x    over 3d  -> 10% gone, slowly.        Ticket.
  #
  # Route `severity: page` to your pager and `severity: ticket` to a queue.
  # If you route tickets to the pager you have rebuilt threshold alerting.
  # ==========================================================================
  - name: temporal-slo-burn
    rules:
      - alert: SLOFastBurn
        expr: |
          (slo:burn_rate:ratio_rate1h > 14.4) and (slo:burn_rate:ratio_rate5m > 14.4)
        for: 2m
        labels:
          severity: page
          component: temporal-slo
        annotations:
          summary: "{{{{ $labels.sli }}}} burning error budget 14.4x (ns={{{{ $labels.namespace }}}})"
          description: "At this rate the entire {COMPLIANCE} budget is gone in about two hours. Sustained over 1h and still active in the last 5m."

      - alert: SLOSlowBurn
        expr: |
          (slo:burn_rate:ratio_rate6h > 6) and (slo:burn_rate:ratio_rate30m > 6)
        for: 15m
        labels:
          severity: page
          component: temporal-slo
        annotations:
          summary: "{{{{ $labels.sli }}}} burning error budget 6x (ns={{{{ $labels.namespace }}}})"
          description: "Slower, still fast enough to exhaust the budget well before the window ends."

      - alert: SLOBudgetBurnTicket
        expr: |
          (slo:burn_rate:ratio_rate1d > 3) and (slo:burn_rate:ratio_rate2h > 3)
        for: 1h
        labels:
          severity: ticket
          component: temporal-slo
        annotations:
          summary: "{{{{ $labels.sli }}}} burning error budget 3x (ns={{{{ $labels.namespace }}}})"
          description: "Not an emergency. Worth a ticket before it becomes one."

      - alert: SLOBudgetBurnSlowTicket
        expr: |
          (slo:burn_rate:ratio_rate3d > 1) and (slo:burn_rate:ratio_rate6h > 1)
        for: 3h
        labels:
          severity: ticket
          component: temporal-slo
        annotations:
          summary: "{{{{ $labels.sli }}}} spending budget faster than sustainable (ns={{{{ $labels.namespace }}}})"
          description: "Above 1x means you will not make the window if this continues. Chronic, not acute."

      - alert: SLOErrorBudgetExhausted
        expr: slo:error_budget_remaining:ratio <= 0
        for: 10m
        labels:
          severity: ticket
          component: temporal-slo
        annotations:
          summary: "{{{{ $labels.sli }}}} has exhausted its {COMPLIANCE} error budget (ns={{{{ $labels.namespace }}}})"
          description: "The SLO has been missed across the compliance window. Under a standard error budget policy this freezes risky change for this component until the budget recovers."
''')

    with open(OUT, "w") as f:
        f.write("".join(out))
    print(f"wrote {OUT}")
    print(f"  {len(SLIS)} SLIs x 2 = {len(SLIS)*2} layer-1 rules")
    print(f"  layer 2: {len(WINDOWS)} ratios + {len(SLIS)} objectives + {len(BURN_WINDOWS)} burn + 5 generic")
    print(f"  layer 3: 5 alerts")


emit()
