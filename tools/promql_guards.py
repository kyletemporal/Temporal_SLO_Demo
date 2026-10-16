"""Outcome-ratio PromQL that keeps resolving when one outcome has no series.

Shared by every rule generator that builds a "bad outcomes / all outcomes"
ratio (workflow_completion, activity_completion). One implementation, because
the four hand-copies this replaces all had the same bug.

THE BUG IT FIXES. Counters like `workflow_failed` do not exist until the first
failure, so each term needs a guard that manufactures a zero-valued series.
The old guard borrowed the SUCCESS series:

    (sum(rate(workflow_failed[5m])) or sum(rate(workflow_success[5m])) * 0)

and so did the denominator (`sum(rate(workflow_success)) + ...`). In a total
outage — every Workflow failing, so `workflow_success` has no series — both
sides came back EMPTY and the alert could not fire. promtool unit test, input
series = failed + timeout only: TemporalWorkflowFailureRatioHigh got [].

THE FIX. Guard every term against the UNION of all terms:

    zero = (S or F or T) * 0          # a 0-series wherever ANY outcome exists
    bad   = (F or zero) + (T or zero)
    total = (S or zero) + (F or zero) + (T or zero)

Why not one regex selector, `rate({__name__=~"workflow_(success|failed)"})`?
rate() drops the metric name, the outcome series then share a label set, and
Prometheus rejects the result: "vector cannot contain metrics with the same
labelset". The union form needs no feature flags.

tests/promql/*.test.yml pin all of this with promtool, including the
no-success-series case.
"""


def outcome_ratio_terms(series, bad, wrap, indent="            "):
    """Return (bad_expr, total_expr) as PromQL strings.

    series  ordered mapping of outcome name -> base expression, e.g.
            {"success": "workflow_success{ns}", "failed": "workflow_failed{ns}"}
    bad     outcome names that count as bad, e.g. ("failed", "timeout")
    wrap    format string turning a base expression into an aggregated rate,
            e.g. "sum by (namespace) (rate({}[5m]))"
    """
    terms = {name: wrap.format(expr) for name, expr in series.items()}
    zero = "(" + " or ".join(terms.values()) + ") * 0"

    def guarded(name):
        return f"({terms[name]} or {zero})"

    sep = f"\n{indent}+\n{indent}"
    bad_expr = "(\n" + indent + sep.join(guarded(n) for n in bad) + "\n" + indent[:-2] + ")"
    total_expr = "(\n" + indent + sep.join(guarded(n) for n in series) + "\n" + indent[:-2] + ")"
    return bad_expr, total_expr
