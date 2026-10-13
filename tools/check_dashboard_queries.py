#!/usr/bin/env python3
"""Run every query in a dashboard against a live Prometheus and report empties.

A dashboard panel that returns nothing looks identical to a system that is
idle. That ambiguity is the single most common way a board loses trust: the
first time someone checks a panel during an incident and it is blank, they stop
believing the rest of it too.

This resolves the dashboard's template variables to a permissive default,
substitutes the Grafana-only macros Prometheus does not understand, and reports
which panels came back with no series.

Usage:
    tools/check_dashboard_queries.py demo/grafana/dashboards/custom/x.json
    tools/check_dashboard_queries.py <file> --prom http://localhost:9090

Exit status is 1 if any query ERRORS. Empty results are reported but do NOT
fail the run on their own: a panel can be legitimately empty (no refusals have
happened, no model is unpriced) and several here are designed to be empty when
things are healthy. Read the output; do not just check the exit code.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request

PROM = "http://localhost:9090"

# Grafana macros Prometheus cannot parse. $__rate_interval is resolved by
# Grafana from the panel's interval and the scrape interval; 1m is a reasonable
# stand-in for a 10s scrape.
MACROS = {
    "$__rate_interval": "1m",
    "$__interval": "1m",
    "$__range": "1h",
}


def resolve(expr: str) -> str:
    for macro, value in MACROS.items():
        expr = expr.replace(macro, value)
    # Template variables become match-everything. A regex matcher with .* is
    # the closest analogue to Grafana's "All" selection and keeps the query
    # syntactically valid.
    expr = re.sub(r"=~\"\$[A-Za-z_][A-Za-z0-9_]*\"", '=~".*"', expr)
    expr = re.sub(r"=\"\$[A-Za-z_][A-Za-z0-9_]*\"", '=~".*"', expr)
    return expr


def query(prom: str, expr: str) -> tuple[bool, int, str]:
    url = prom + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = json.load(resp)
    except Exception as exc:  # noqa: BLE001 — surfaced in the report
        return False, 0, str(exc)

    if body.get("status") != "success":
        return False, 0, body.get("error", "unknown error")
    return True, len(body["data"]["result"]), ""


def walk_panels(panels: list) -> list:
    out = []
    for p in panels:
        out.append(p)
        out.extend(walk_panels(p.get("panels", [])))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    path = sys.argv[1]
    prom = PROM
    if "--prom" in sys.argv:
        prom = sys.argv[sys.argv.index("--prom") + 1]

    dash = json.loads(open(path).read())
    panels = [p for p in walk_panels(dash["panels"]) if p.get("type") != "row"]

    errors, empties, ok, skipped = [], [], 0, 0
    for panel in panels:
        for target in panel.get("targets", []):
            expr = target.get("expr")
            if not expr:
                continue
            # Skip panels that are not Prometheus.
            #
            # A LogQL query sent to Prometheus returns HTTP 400, and this tool
            # reported that as "a bug, not an idle panel" — which is exactly
            # backwards: the panel is fine, the checker sent it to the wrong
            # engine. Measured on the golden-signals board, where 'Find stuck
            # executions (workflow IDs)' is a Loki panel and was the only
            # "error" on an entirely healthy dashboard.
            #
            # demo/scripts/validate.sh routes these to Loki instead of skipping
            # them. This tool is deliberately Prometheus-only and single-file,
            # so it reports the skip rather than growing a second client.
            ds = target.get("datasource") or panel.get("datasource") or {}
            if isinstance(ds, dict) and ds.get("type") not in (None, "prometheus"):
                skipped += 1
                continue
            good, count, err = query(prom, resolve(expr))
            label = f"{panel['title']!r} [{target.get('refId', '?')}]"
            if not good:
                errors.append((label, err, resolve(expr)))
            elif count == 0:
                empties.append(label)
            else:
                ok += 1

    print(f"{path}")
    skip_note = f", {skipped} skipped (non-Prometheus)" if skipped else ""
    print(f"  {ok} returning data, {len(empties)} empty, "
          f"{len(errors)} errored{skip_note}\n")

    if errors:
        print("ERRORED — these are bugs, not idle panels:")
        for label, err, expr in errors:
            print(f"  {label}\n    {err}\n    {expr}\n")

    if empties:
        print("EMPTY — verify each is legitimately empty rather than mistyped:")
        for label in empties:
            print(f"  {label}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
