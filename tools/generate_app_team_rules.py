#!/usr/bin/env python3
"""Minimum SLO rules for an application team building on a Temporal platform."""
import pathlib

OUT = str(pathlib.Path(__file__).resolve().parent.parent / "app-team/prometheus/slo-rules.yml")

WINDOWS = ["5m", "30m", "1h", "6h", "28d"]
BURN_WINDOWS = ["5m", "30m", "1h", "6h"]
COMPLIANCE = "28d"

# Tunables — and the two ways these go wrong, both measured on a real SDK:
#
# 1. `le` IS A STRING MATCH. The Go SDK emits le="1.0", so a selector written
#    le="1" matches NOTHING and the SLI silently produces no series at all —
#    not a wrong number, no number. Write the boundary exactly as the exporter
#    prints it.
#
# 2. THE BOUNDARY MUST ACTUALLY EXIST. The SDK's default histogram buckets are
#    0.001 0.002 0.005 0.01 0.02 0.05 0.1 0.2 0.5 1.0 2.0 5.0 10.0 +Inf
#    — they TOP OUT AT 10 SECONDS. A 60-second latency SLO is not expressible
#    against the defaults; you must configure custom buckets in your Worker
#    first. Check yours before changing these:
#
#      curl -s localhost:<metrics-port>/metrics \
#        | grep temporal_workflow_endtoend_latency_seconds_bucket | grep -o 'le="[^"]*"' | sort -u
S2S_LE = "1.0"   # Activity Task started within N seconds
E2E_LE = "10.0"  # Workflow finished end-to-end within N seconds

# Everything is scoped to YOUR namespace and task queue. A platform Prometheus
# carries every tenant's series; without this scope your SLO silently includes
# somebody else's Workflows.
SCOPE = 'namespace="$NAMESPACE", task_queue="$TASK_QUEUE"'

SLIS = [
    dict(name="workflow_completion", objective=0.99,
         desc="Workflow Executions that completed successfully",
         # temporal_workflow_failed_total DOES NOT EXIST until the first
         # failure. The `or ... * 0` guard manufactures a zero-valued series
         # with matching labels so the ratio resolves instead of vanishing.
         bad=('(\n'
              f'            sum(rate(temporal_workflow_failed_total{{{SCOPE}}}[5m]))\n'
              '            or\n'
              f'            sum(rate(temporal_workflow_completed_total{{{SCOPE}}}[5m])) * 0\n'
              '          )'),
         total=('(\n'
                f'            sum(rate(temporal_workflow_completed_total{{{SCOPE}}}[5m]))\n'
                '            +\n'
                '            (\n'
                f'              sum(rate(temporal_workflow_failed_total{{{SCOPE}}}[5m]))\n'
                '              or\n'
                f'              sum(rate(temporal_workflow_completed_total{{{SCOPE}}}[5m])) * 0\n'
                '            )\n'
                '          )')),

    dict(name="task_delivery", objective=0.99,
         desc=f"Activity Tasks picked up by a Worker within {S2S_LE}s of scheduling",
         bad=('clamp_min(\n'
              f'            sum(rate(temporal_activity_schedule_to_start_latency_seconds_count{{{SCOPE}}}[5m]))\n'
              '            -\n'
              f'            sum(rate(temporal_activity_schedule_to_start_latency_seconds_bucket{{{SCOPE}, le="{S2S_LE}"}}[5m])),\n'
              '            0\n'
              '          )'),
         total=f'sum(rate(temporal_activity_schedule_to_start_latency_seconds_count{{{SCOPE}}}[5m]))'),

    dict(name="workflow_latency", objective=0.95,
         desc=f"Workflows finishing end-to-end within {E2E_LE}s",
         bad=('clamp_min(\n'
              f'            sum(rate(temporal_workflow_endtoend_latency_seconds_count{{{SCOPE}}}[5m]))\n'
              '            -\n'
              f'            sum(rate(temporal_workflow_endtoend_latency_seconds_bucket{{{SCOPE}, le="{E2E_LE}"}}[5m])),\n'
              '            0\n'
              '          )'),
         total=f'sum(rate(temporal_workflow_endtoend_latency_seconds_count{{{SCOPE}}}[5m]))'),
]

HEADER = f'''# =============================================================================
# MINIMUM SLOs for a Temporal application team
# =============================================================================
#
# Three SLIs. That is the whole standard, and it is deliberately small — a team
# that maintains three meaningful SLOs is far better off than one that inherits
# thirty and reads none.
#
#   workflow_completion   did the work finish?      <- your users feel this
#   task_delivery         did Workers keep up?      <- your capacity
#   workflow_latency      was it fast enough?       <- your promise
#
# ---------------------------------------------------------------------------
# BEFORE THIS WILL WORK — two substitutions
# ---------------------------------------------------------------------------
#   $NAMESPACE    your Temporal Namespace
#   $TASK_QUEUE   your Task Queue
#
#   ./scripts/configure.sh my-team-prod orders
#
# Use the script, not sed: `sed -i` needs a backup-suffix argument on BSD/macOS
# and the naive form fails there silently enough to waste an afternoon.
#
# The scope is not optional. A platform Prometheus carries every tenant's
# series; unscoped, your SLO quietly measures somebody else's Workflows and
# you page on their incident.
#
# ---------------------------------------------------------------------------
# Record names are prefixed `appslo:`
# ---------------------------------------------------------------------------
# So your rules can live in the same Prometheus as the platform team's without
# either set overwriting the other.
#
# ---------------------------------------------------------------------------
# These come from YOUR Workers
# ---------------------------------------------------------------------------
# Every metric here is emitted by your own Worker process. If your Workers stop,
# these series STOP EXISTING — they do not go to zero. A threshold alert cannot
# detect that; only an absence alert can, which is why alerts.yml ships one.
#
# Ask your platform team to also expose the cluster-side view of your Namespace
# (workflow_success / workflow_failed / no_poller_tasks). Those keep reporting
# when your Workers are down and are the backstop for exactly this blind spot.
#
# ---------------------------------------------------------------------------
# The objectives are placeholders
# ---------------------------------------------------------------------------
# 99% / 99% / 95% are round numbers. Run these for two weeks in recording-only
# mode, look at what you actually deliver, then promise that. Tunables at the
# top of the generator: S2S_LE={S2S_LE}s, E2E_LE={E2E_LE}s.
# =============================================================================

groups:
'''


def emit():
    out = [HEADER]
    out.append("  - name: temporal-app-slo-events\n    interval: 30s\n    rules:\n")
    for s in SLIS:
        out.append(f"\n      # --- {s['name']}: {s['desc']}\n")
        out.append("      - record: appslo:events_bad:rate5m\n        expr: |\n")
        out.append(f"          {s['bad']}\n        labels:\n          sli: {s['name']}\n")
        out.append("      - record: appslo:events_total:rate5m\n        expr: |\n")
        out.append(f"          {s['total']}\n        labels:\n          sli: {s['name']}\n")

    out.append("\n  - name: temporal-app-slo-meta\n    interval: 30s\n    rules:\n")
    for w in WINDOWS:
        out.append(f"      - record: appslo:sli_bad:ratio_rate{w}\n        expr: |\n")
        if w == "5m":
            out.append("          appslo:events_bad:rate5m\n          /\n          appslo:events_total:rate5m\n")
        else:
            out.append(f"          avg_over_time(appslo:events_bad:rate5m[{w}])\n")
            out.append(f"          /\n          avg_over_time(appslo:events_total:rate5m[{w}])\n")

    for s in SLIS:
        out.append(f"      - record: appslo:objective:ratio\n        expr: vector({s['objective']})\n")
        out.append(f"        labels:\n          sli: {s['name']}\n")

    out.append("      - record: appslo:error_budget:ratio\n        expr: 1 - appslo:objective:ratio\n")
    out.append(f"      - record: appslo:compliance_window_bad:ratio\n        expr: appslo:sli_bad:ratio_rate{COMPLIANCE}\n")
    for w in BURN_WINDOWS:
        out.append(f"      - record: appslo:burn_rate:ratio_rate{w}\n        expr: |\n")
        out.append(f"          appslo:sli_bad:ratio_rate{w}\n            / on(sli) group_left() appslo:error_budget:ratio\n")
    out.append("      - record: appslo:error_budget_remaining:ratio\n        expr: |\n")
    out.append("          1 - (\n            appslo:compliance_window_bad:ratio\n")
    out.append("              / on(sli) group_left() appslo:error_budget:ratio\n          )\n")
    out.append("      - record: appslo:sli_good:ratio\n        expr: 1 - appslo:compliance_window_bad:ratio\n")

    out.append('''
  # ==========================================================================
  # Two burn-rate alerts. Minimum useful set.
  #
  # Two windows must breach together: the long one proves the burn is real, the
  # short one proves it is still happening, so the alert clears when you fix it
  # rather than lingering for an hour.
  # ==========================================================================
  - name: temporal-app-slo-burn
    rules:
      - alert: AppSLOFastBurn
        expr: |
          (appslo:burn_rate:ratio_rate1h > 14.4) and (appslo:burn_rate:ratio_rate5m > 14.4)
        for: 2m
        labels: {severity: page, component: temporal-app}
        annotations:
          summary: "{{ $labels.sli }} is burning error budget 14.4x too fast"
          description: "At this rate the whole 28-day budget is gone in about two hours."

      - alert: AppSLOErrorBudgetExhausted
        expr: appslo:error_budget_remaining:ratio <= 0
        for: 15m
        labels: {severity: ticket, component: temporal-app}
        annotations:
          summary: "{{ $labels.sli }} has spent its 28-day error budget"
          description: "You have missed this SLO across the compliance window. Under a standard error budget policy that means slowing risky change until it recovers."
''')

    pathlib.Path(OUT).parent.mkdir(parents=True, exist_ok=True)
    open(OUT, "w").write("".join(out))
    print(f"wrote {OUT}\n  {len(SLIS)} SLIs — deliberately minimum")


emit()
