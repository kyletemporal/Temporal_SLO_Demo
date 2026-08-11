#!/usr/bin/env python3
"""Generate prometheus/slo-rules.yml.

Every SLI is expressed as a BAD-EVENT RATIO (bad / total), because error budget
math is defined on bad events. Availability = 1 - bad_ratio.
"""
import pathlib

OUT = str(pathlib.Path(__file__).resolve().parent.parent / "demo/prometheus/slo-rules.yml")

WINDOWS = ["5m", "30m", "1h", "6h"]

# The `or <total> * 0` guard appears in every ratio on purpose. If the error
# counter has never been incremented its series does not exist, and
# `missing / total` returns NOTHING rather than 0 — the SLI silently vanishes
# instead of reporting "perfect". Worse, in the inverse case (all traffic
# failing, no successes) the naive form also returns nothing, so a total outage
# produces no signal at all.

# Error types that are the CALLER's fault, not the service's. Counting these
# against an availability SLO is the most common way an SLI ends up measuring
# something nobody cares about. Measured on this cluster at idle: matching emits
# a steady ~0.39/s of serviceerror_Canceled on AddWorkflowTask/AddActivityTask
# from routine context cancellation, which alone drove matching availability to
# 98.77% and blew an entire 99.9% error budget while nothing was wrong.
#
# Keep Unavailable, Internal, ResourceExhausted, DataLoss — those are real.
CLIENT_FAULTS = (
    'error_type!~"serviceerror_(Canceled|NotFound|NamespaceNotFound'
    '|AlreadyExist.*|InvalidArgument|FailedPrecondition'
    '|WorkflowExecutionAlreadyStarted|QueryFailed)"'
)

# Long-poll operations block for up to 60 SECONDS by design — that is how
# Workers wait for Tasks. They are also the highest-volume operations on the
# Frontend (PollWorkflowTaskQueue 7.2/s and PollActivityTaskQueue 5.6/s here,
# out of ~26/s total). Including them in a latency SLI means the number is
# dominated by how long Workers idle, which has nothing to do with whether the
# service is fast. Measured: including them reported 95.9% under 500ms;
# excluding them reports the real figure.
#
# `Poll.*` alone is NOT enough. Two matching operations are long-poll watches
# that do not start with "Poll", and they are invisible under load and glaring
# at idle. Measured over 2h on this stack, fraction of calls slower than 500ms:
#
#   matching   ListNexusEndpoints      100.00%
#   frontend   PollActivityTaskQueue   100.00%
#   frontend   PollWorkflowTaskQueue   100.00%
#   matching   PollWorkflowTaskQueue   100.00%
#   matching   GetTaskQueueUserData     95.32%
#   matching   PollActivityTaskQueue    92.76%
#   ...everything else                  < 1%
#
# With only `Poll.*` excluded, GetTaskQueueUserData is 96% of matching's
# remaining traffic on an idle cluster and drove matching_latency to 4.8%
# attainment — a 9,400% error budget overspend on a system doing nothing wrong.
#
# Re-derive this list on your own cluster rather than trusting it; operation
# names change between versions. The discovery query is in docs/SLO-GUIDE.md.
NOT_LONG_POLL = 'operation!~"Poll.*|GetTaskQueueUserData|ListNexusEndpoints"'


def err_over_req(svc):
    return (
        '(\n'
        '            sum(rate(service_error_with_type{{service_name="{svc}",{flt}}}[{{w}}]))\n'
        '            or\n'
        '            sum(rate(service_requests{{service_name="{svc}"}}[{{w}}])) * 0\n'
        '          )\n'
        '          /\n'
        '          sum(rate(service_requests{{service_name="{svc}"}}[{{w}}]))'
    ).format(svc=svc, flt=CLIENT_FAULTS)


def slow_requests(svc, le):
    return (
        '1 - (\n'
        '            sum(rate(service_latency_bucket{{service_name="{svc}",{np},le="{le}"}}[{{w}}]))\n'
        '            /\n'
        '            sum(rate(service_latency_count{{service_name="{svc}",{np}}}[{{w}}]))\n'
        '          )'
    ).format(svc=svc, le=le, np=NOT_LONG_POLL)


SLIS = [
    dict(name="frontend_availability", objective=0.999, by=None,
         desc="Frontend gRPC requests that did not return an error",
         expr=err_over_req("frontend")),
    dict(name="frontend_latency", objective=0.99, by=None,
         desc="Frontend gRPC requests served in under 500ms",
         expr=slow_requests("frontend", "0.5")),
    dict(name="history_availability", objective=0.999, by=None,
         desc="History service requests that did not return an error",
         expr=err_over_req("history")),
    dict(name="history_latency", objective=0.99, by=None,
         desc="History service requests served in under 500ms",
         expr=slow_requests("history", "0.5")),
    dict(name="matching_availability", objective=0.999, by=None,
         desc="Matching service requests that did not return an error",
         expr=err_over_req("matching")),
    dict(name="matching_latency", objective=0.99, by=None,
         desc="Matching service requests served in under 500ms",
         expr=slow_requests("matching", "0.5")),
    # Keeps the service_name label, so this single SLI definition yields one
    # SLO per Temporal service role — including `worker` and `server`, which
    # serve no gRPC traffic of their own and would otherwise have no SLO at all.
    dict(name="persistence_availability", objective=0.999, by="service_name",
         desc="Persistence operations per service role that did not error",
         expr=(
             '(\n'
             '            sum by (service_name) (rate(persistence_error_with_type{' + CLIENT_FAULTS + '}[{w}]))\n'
             '            or\n'
             '            sum by (service_name) (rate(persistence_requests[{w}])) * 0\n'
             '          )\n'
             '          /\n'
             '          sum by (service_name) (rate(persistence_requests[{w}]))'
         )),
    # SDK-side. Measures the Worker fleet rather than the cluster: how often a
    # Task waited too long for a free Worker.
    # Objective is 90%, not 99%, and that is a measurement rather than a
    # concession. This lab pins MAX_CONCURRENT_ACTIVITIES=10 (Temporal's real
    # default is 1000) so that slot exhaustion is reachable on a laptop. At
    # baseline that config delivers only 94.4% of Activity Tasks within 200ms —
    # 99.9% make it within 500ms, so the fleet is not broken, it is small.
    #
    # Setting 99% here would put the board permanently in breach at baseline,
    # which trains people to ignore it. The honest options are to promise what
    # the current configuration can deliver, or to change the configuration:
    # `MAX_CONCURRENT_ACTIVITIES=200 docker compose up -d worker` takes this SLI
    # to ~100% on the same hardware. That trade is the whole of Scenario 4.
    dict(name="worker_task_delivery", objective=0.90, by=None,
         desc="Activity Tasks picked up by a Worker within 200ms of scheduling",
         expr=(
             '1 - (\n'
             '            sum(rate(temporal_activity_schedule_to_start_latency_seconds_bucket{le="0.2"}[{w}]))\n'
             '            /\n'
             '            sum(rate(temporal_activity_schedule_to_start_latency_seconds_count[{w}]))\n'
             '          )'
         )),
    # Application-level, from cluster metrics (history service), so it keeps
    # reporting even when the Worker fleet is entirely down.
    # Counts TIMEOUTS as bad, not just failures. A Workflow that hit its
    # WorkflowExecutionTimeout did not succeed, and treating it as a non-event
    # leaves a hole exactly where the orphaned-Task-Queue failure mode lives:
    # work sent to a queue nobody polls never fails, it just expires.
    # workflow_cancel is deliberately NOT counted — a cancellation is somebody
    # getting what they asked for.
    dict(name="workflow_completion", objective=0.99, by=None,
         desc="Workflow Executions that completed successfully (timeouts count as bad)",
         expr=(
             '(\n'
             '            (sum(rate(workflow_failed[{w}]))  or sum(rate(workflow_success[{w}])) * 0)\n'
             '            +\n'
             '            (sum(rate(workflow_timeout[{w}])) or sum(rate(workflow_success[{w}])) * 0)\n'
             '          )\n'
             '          /\n'
             '          clamp_min(\n'
             '            sum(rate(workflow_success[{w}]))\n'
             '            +\n'
             '            (sum(rate(workflow_failed[{w}]))  or sum(rate(workflow_success[{w}])) * 0)\n'
             '            +\n'
             '            (sum(rate(workflow_timeout[{w}])) or sum(rate(workflow_success[{w}])) * 0),\n'
             '            0.001\n'
             '          )'
         )),
]

HEADER = '''# =============================================================================
# Temporal SLO definitions, error budgets, and burn-rate alerts
# =============================================================================
#
# GENERATED-STYLE FILE: the per-window rules below are mechanical repetition of
# nine SLI definitions across four rate windows. Edit the SLI list, not the
# individual rules, or they will drift apart.
#
# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------
# Every SLI is recorded as a BAD-EVENT RATIO: bad events / total events.
# Availability is 1 - that. Error budget math is defined on bad events, so
# recording the bad ratio directly keeps every downstream rule trivial.
#
#   slo:sli_bad:ratio_rate<window>   per-SLI bad ratio            (layer 1)
#   slo:objective:ratio              the SLO target, e.g. 0.999   (layer 2)
#   slo:error_budget:ratio           1 - objective                (layer 2)
#   slo:burn_rate:ratio_rate<window> bad ratio / error budget     (layer 2)
#   slo:error_budget_remaining:ratio 1 - consumed budget          (layer 2)
#
# Layer 2 is written ONCE and applies to every SLI by matching on the `sli`
# label. Adding a new SLI means adding layer-1 rules only.
#
# A burn rate of 1 means you will consume exactly 100% of the error budget by
# the end of the compliance window. A burn rate of 14.4 means you will consume
# it in 1/14.4 of the window — about 2 hours of a 30-day budget.
#
# ---------------------------------------------------------------------------
# COMPLIANCE WINDOW — read this before trusting the budget numbers
# ---------------------------------------------------------------------------
# This lab uses a 1 HOUR compliance window, not the 28-30 days you would use in
# production. That is deliberate: a 10-minute chaos scenario visibly drains a
# 1-hour budget, which is the entire teaching point. Against a 30-day budget the
# same scenario moves the number by 0.02% and demonstrates nothing.
#
# To switch to production semantics:
#   1. Add `1h`-style rules for a 28d window in the layer-1 group below.
#   2. Repoint the `slo:compliance_window_bad:ratio` alias in layer 2 at it.
#   3. Raise Prometheus retention past 28d (docker-compose.yml) AND give it
#      durable storage. Six hours of retention cannot answer a 30-day question,
#      and a restart resets your error budget to pristine — which is the single
#      most common way SLO dashboards end up lying.
#
# ---------------------------------------------------------------------------
# Objectives are PLACEHOLDERS
# ---------------------------------------------------------------------------
# 99.9% and 99% are round numbers, not measurements. A real SLO is derived from
# what users need and what the system has historically delivered. Run
# `make baseline` for a week before promoting any of these, or you will page
# yourself for behaviour the system has always had.
# =============================================================================

groups:
'''


def emit():
    out = [HEADER]

    # ---------------- Layer 1: per-SLI bad ratios -----------------
    out.append("  # ==========================================================================\n")
    out.append("  # LAYER 1 — SLI bad-event ratios, one rule per SLI per window\n")
    out.append("  # ==========================================================================\n")
    out.append("  - name: temporal-slo-sli\n")
    out.append("    interval: 30s\n")
    out.append("    rules:\n")
    for s in SLIS:
        out.append(f"\n      # --- {s['name']}: {s['desc']}\n")
        for w in WINDOWS:
            # replace(), not format(): by this point the expression contains
            # literal single-brace label selectors like {service_name="frontend"}
            # which str.format would try to interpret as fields.
            expr = s["expr"].replace("{w}", w)
            out.append(f"      - record: slo:sli_bad:ratio_rate{w}\n")
            out.append(f"        expr: |\n")
            out.append(f"          {expr}\n")
            out.append(f"        labels:\n")
            out.append(f"          sli: {s['name']}\n")

    # ---------------- Layer 2: objectives -----------------
    out.append("\n  # ==========================================================================\n")
    out.append("  # LAYER 2 — objectives, budgets, burn rates. Generic: matches on `sli`.\n")
    out.append("  # ==========================================================================\n")
    out.append("  - name: temporal-slo-meta\n")
    out.append("    interval: 30s\n")
    out.append("    rules:\n")
    out.append("\n      # The objective for each SLI, as a constant series.\n")
    for s in SLIS:
        out.append(f"      - record: slo:objective:ratio\n")
        out.append(f"        expr: vector({s['objective']})\n")
        out.append(f"        labels:\n")
        out.append(f"          sli: {s['name']}\n")

    out.append("\n      # Error budget = the fraction of bad events the objective permits.\n")
    out.append("      - record: slo:error_budget:ratio\n")
    out.append("        expr: 1 - slo:objective:ratio\n")

    out.append("\n      # Alias marking which window the budget is measured over.\n")
    out.append("      # Repoint this single rule to change the compliance window.\n")
    out.append("      - record: slo:compliance_window_bad:ratio\n")
    out.append("        expr: slo:sli_bad:ratio_rate1h\n")

    out.append("\n      # Burn rate: multiples of budget-consumption pace. 1 = exactly on\n")
    out.append("      # pace to exhaust the budget at the end of the window.\n")
    out.append("      # group_left() because persistence_availability carries an extra\n")
    out.append("      # service_name label that the objective series does not.\n")
    for w in WINDOWS:
        out.append(f"      - record: slo:burn_rate:ratio_rate{w}\n")
        out.append(f"        expr: |\n")
        out.append(f"          slo:sli_bad:ratio_rate{w}\n")
        out.append(f"            / on(sli) group_left() slo:error_budget:ratio\n")

    out.append("\n      # Fraction of the error budget still unspent. Goes NEGATIVE when the\n")
    out.append("      # SLO has been missed for the window — that is a feature, not a bug:\n")
    out.append("      # -0.5 means you burned 150% of the budget and owes a conversation\n")
    out.append("      # about slowing feature work.\n")
    out.append("      - record: slo:error_budget_remaining:ratio\n")
    out.append("        expr: |\n")
    out.append("          1 - (\n")
    out.append("            slo:compliance_window_bad:ratio\n")
    out.append("              / on(sli) group_left() slo:error_budget:ratio\n")
    out.append("          )\n")

    out.append("\n      # Attained availability over the compliance window, for display.\n")
    out.append("      - record: slo:sli_good:ratio\n")
    out.append("        expr: 1 - slo:compliance_window_bad:ratio\n")

    out.append("\n      # The objective, re-broadcast onto the FULL label set of each SLI.\n")
    out.append("      # slo:objective:ratio carries only `sli`, but persistence_availability\n")
    out.append("      # series also carry service_name. A dashboard table joins columns by\n")
    out.append("      # identical labels, so mixing the two label shapes splits every\n")
    out.append("      # persistence row in half with nulls on both sides. Multiplying the\n")
    out.append("      # SLI by zero and adding the objective copies the value onto the\n")
    out.append("      # wider label set.\n")
    out.append("      - record: slo:objective_expanded:ratio\n")
    out.append("        expr: |\n")
    out.append("          slo:compliance_window_bad:ratio * 0\n")
    out.append("            + on(sli) group_left() slo:objective:ratio\n")

    # ---------------- Layer 3: burn alerts -----------------
    out.append("""
  # ==========================================================================
  # LAYER 3 — multi-window, multi-burn-rate alerts
  #
  # Two windows per alert, both of which must be breaching. The long window
  # establishes that the burn is real; the short window establishes that it is
  # still happening right now, so the alert resolves promptly once the incident
  # ends instead of hanging around for the length of the long window.
  #
  # This replaces static threshold paging. A static "error rate > 1%" alert
  # fires identically for a 90-second blip and a sustained outage; a burn-rate
  # alert fires in proportion to how much of the budget the event is actually
  # eating.
  #
  # 14.4x over 1h  -> 2% of a 30-day budget consumed. Page.
  # 6x over 6h     -> 5% of a 30-day budget consumed. Page, less urgently.
  # ==========================================================================
  - name: temporal-slo-burn
    rules:
      - alert: SLOFastBurn
        expr: |
          (slo:burn_rate:ratio_rate1h > 14.4)
          and
          (slo:burn_rate:ratio_rate5m > 14.4)
        for: 2m
        labels:
          severity: critical
          component: temporal-slo
        annotations:
          summary: "{{ $labels.sli }} is burning error budget 14.4x too fast"
          description: "At this rate the entire error budget for {{ $labels.sli }} is gone in a fraction of the compliance window. Sustained over 1h and still active in the last 5m."

      - alert: SLOSlowBurn
        expr: |
          (slo:burn_rate:ratio_rate6h > 6)
          and
          (slo:burn_rate:ratio_rate30m > 6)
        for: 15m
        labels:
          severity: warning
          component: temporal-slo
        annotations:
          summary: "{{ $labels.sli }} is burning error budget 6x too fast"
          description: "A slower burn that will still exhaust the budget well before the window ends. Investigate before it becomes a fast burn."

      - alert: SLOErrorBudgetExhausted
        expr: slo:error_budget_remaining:ratio <= 0
        for: 5m
        labels:
          severity: warning
          component: temporal-slo
        annotations:
          summary: "{{ $labels.sli }} has exhausted its error budget"
          description: "The SLO has been missed across the compliance window. Under a standard error budget policy this freezes risky changes for this component until the budget recovers."
""")
    with open(OUT, "w") as f:
        f.write("".join(out))
    n_l1 = len(SLIS) * len(WINDOWS)
    print(f"wrote {OUT}")
    print(f"  layer 1: {n_l1} recording rules ({len(SLIS)} SLIs x {len(WINDOWS)} windows)")
    print(f"  layer 2: {len(SLIS)} objectives + {len(WINDOWS)} burn rates + 4 generic")
    print(f"  layer 3: 3 alerts")


emit()
