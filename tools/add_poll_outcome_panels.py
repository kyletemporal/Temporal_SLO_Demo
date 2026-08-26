#!/usr/bin/env python3
"""Add row P — Poll outcomes — to the demo Golden Signals dashboard.

Supports scenario 8 (`make chaos-poller-flood`), the only chaos scenario that
runs the Worker fleet in the OPPOSITE direction from the other seven.

WHY THIS IS A SEPARATE SCRIPT AND NOT PART OF generate_golden_signals.py
The row is scenario-specific and demo-only. generate_golden_signals.py writes
BOTH demo/ and production/, and these panels have no business on a production
board — they exist to teach a contrast between two chaos scenarios.

ORDERING MATTERS, AND IT IS THE FRAGILE PART:

    python3 tools/generate_golden_signals.py demo/grafana/dashboards/slo/temporal-golden-signals.json
    python3 tools/add_poll_outcome_panels.py

generate_golden_signals.py REWRITES the file from scratch, so re-running it
silently drops this row. Always run this script after it. Running this script
twice is safe — it is idempotent.

Idempotency is by marker, not by title: every panel it owns carries
`_pollOutcomeOwned: true`, and a re-run removes all marked panels before
re-adding them. Matching on title would orphan a panel the moment one is
renamed, leaving a duplicate behind.

Usage:
    python3 tools/add_poll_outcome_panels.py [dashboard.json]
"""
import json, pathlib, sys

DEFAULT = (pathlib.Path(__file__).resolve().parent.parent
           / "demo/grafana/dashboards/slo/temporal-golden-signals.json")
PATH = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT

MARKER = "_pollOutcomeOwned"

# Well clear of the generator's range (it ends in the 30s). If the generator
# ever grows this far, the collision check below stops rather than clobbers.
BASE_ID = 300

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}

GOOD, SERIOUS, CRITICAL = "#0ca30c", "#ec835a", "#d03b3b"
C1, C2, C3, C5 = "#3987e5", "#d95926", "#199e70", "#d55181"


def tgt(expr, legend, ref="A"):
    return {"datasource": DS, "editorMode": "code", "expr": expr,
            "range": True, "instant": False, "refId": ref, "legendFormat": legend}


def base(kind, pid, title, gp, desc):
    return {"type": kind, "title": title, "id": pid, "datasource": DS,
            "gridPos": gp, "description": desc, MARKER: True}


def ts(pid, title, gp, targets, unit, desc, overrides=None, stack=False,
       thr=None, minv=None, maxv=None, decimals=None, fill=14,
       legend_mode="table", calcs=None):
    d = {"color": {"mode": "thresholds" if thr else "palette-classic"},
         "unit": unit,
         "custom": {"lineWidth": 2, "fillOpacity": fill, "showPoints": "never",
                    "spanNulls": False, "gradientMode": "opacity",
                    "lineInterpolation": "smooth", "axisSoftMin": 0}}
    if stack:
        d["custom"]["stacking"] = {"group": "A", "mode": "normal"}
        d["custom"]["fillOpacity"] = 80
        d["custom"]["lineWidth"] = 1
    if thr: d["thresholds"] = thr
    if minv is not None: d["min"] = minv
    if maxv is not None: d["max"] = maxv
    if decimals is not None: d["decimals"] = decimals
    p = base("timeseries", pid, title, gp, desc)
    p["targets"] = targets
    p["fieldConfig"] = {"defaults": d, "overrides": overrides or []}
    p["options"] = {"legend": {"displayMode": legend_mode, "placement": "bottom",
                               "showLegend": True,
                               "calcs": calcs or ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}}
    return p


def colour(name, hexv, axis=None, unit=None, label=None):
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": hexv}}]
    if axis: props.append({"id": "custom.axisPlacement", "value": axis})
    if unit: props.append({"id": "unit", "value": unit})
    if label: props.append({"id": "custom.axisLabel", "value": label})
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


# ---------------------------------------------------------------------------
# The panels. Descriptions carry the teaching — on this dashboard they ARE the
# documentation, and they are what someone reads at 2am instead of the runbook.
# ---------------------------------------------------------------------------
def build(y0):
    P = []

    row = base("row", BASE_ID, "P — Poll outcomes  ·  is the fleet the right SIZE?",
               {"h": 1, "w": 24, "x": 0, "y": y0}, "")
    row["collapsed"] = False
    row["panels"] = []
    del row["datasource"]
    P.append(row)

    P.append(ts(
        BASE_ID + 1, "Poll Outcome Mix",
        {"h": 8, "w": 12, "x": 0, "y": y0 + 1},
        [tgt("sum(temporal:poll_sync:rate5m)", "sync match", "A"),
         tgt("sum(temporal:poll_async:rate5m)", "async match", "B"),
         tgt("sum(temporal:poll_empty:rate5m)", "empty (timed out)", "C")],
        "reqps",
        "EVERY POLL ENDS IN EXACTLY ONE OF THESE THREE, so the stack is the "
        "complete picture of what your pollers are doing.\n\n"
        "  sync match  — a Worker was waiting; the Task went straight to it. Best case.\n"
        "  async match — the Task was delivered, but had to be PERSISTED first "
        "because no Worker was ready. Growing band = starvation.\n"
        "  empty       — the poll waited its full 60s and found nothing. "
        "Growing band = over-provisioning.\n\n"
        "ASYNC MATCH IS A SUBTRACTION, NOT A COUNTER. Temporal exposes no "
        "async-match metric; it is poll_success - poll_success_sync, clamped at "
        "zero for the scrape skew where the two counters land in different "
        "windows.\n\n"
        "poll_timeouts is NOT async match. It counts polls that found NOTHING. "
        "Reading it as async match inverts the conclusion entirely — a flooded "
        "fleet would read as a starved one.\n\n"
        "Scenario 1 (backlog) and 4 (slots) grow the ASYNC band.\n"
        "Scenario 8 (poller flood) grows the EMPTY band.",
        stack=True,
        overrides=[colour("sync match", GOOD),
                   colour("async match", SERIOUS),
                   colour("empty (timed out)", C1)]))

    P.append(ts(
        BASE_ID + 2, "Sync Match Rate vs Poll Success Rate",
        {"h": 8, "w": 12, "x": 12, "y": y0 + 1},
        [tgt("max(temporal:sync_match_rate:ratio5m)", "sync match rate", "A"),
         tgt("max(temporal:poll_success_rate:ratio5m)", "poll success rate", "B")],
        "percentunit",
        "TWO RATIOS THAT LOOK ALIKE AND MEASURE UNRELATED THINGS. They are on "
        "one axis deliberately, because the whole lesson is that they diverge.\n\n"
        "SYNC MATCH RATE — sync / all delivered. 'Was a Worker waiting?' A "
        "HEALTH signal. It has no benign low state, which is why "
        "TemporalSyncMatchRateLow needs no second condition (only a volume "
        "guard for the idle case).\n\n"
        "POLL SUCCESS RATE — matched / (matched + empty). 'Did polls find "
        "work?' A SIZING signal. It has TWO opposite low states:\n"
        "  low + schedule-to-start HIGH → starved. Add Workers.\n"
        "  low + schedule-to-start ~0   → flooded. REMOVE Workers.\n\n"
        "That ambiguity is why nothing pages on this alone. "
        "TemporalMatchingStarved requires schedule-to-start as a second "
        "condition; the over-provisioned case is "
        "TemporalWorkerFleetOverProvisioned at severity INFO, because it is a "
        "cost finding rather than an incident.\n\n"
        "During `make chaos-poller-flood`, poll success rate collapses and sync "
        "match rate does NOT move. That divergence is the finding.",
        minv=0, maxv=1, decimals=3, fill=8,
        overrides=[colour("sync match rate", GOOD),
                   colour("poll success rate", C5)]))

    P.append(ts(
        BASE_ID + 3, "Discriminator — schedule-to-start P99 and free slots",
        {"h": 8, "w": 12, "x": 0, "y": y0 + 9},
        [tgt("max(temporal:activity_schedule_to_start:p99_5m)",
             "schedule-to-start p99", "A"),
         tgt('sum(temporal_worker_task_slots_available{namespace="default"})',
             "slots available", "B")],
        "s",
        "THE TWO READINGS THAT SETTLE STARVED vs OVER-PROVISIONED IN ONE GLANCE, "
        "when poll success rate has already told you something is off but not "
        "which direction.\n\n"
        "  s2s CLIMBING + slots at ZERO  → starved. Tasks are waiting. "
        "Scenarios 1 and 4.\n"
        "  s2s ~0 + slots FREE           → NOT starved. Nothing is waiting; the "
        "low poll success rate is idle pollers. Scenario 8.\n\n"
        "Two axes and two units on purpose: seconds on the left, a count on the "
        "right. Sharing an axis would squash schedule-to-start flat against a "
        "slot count in the thousands, which is exactly when you need to read it.\n\n"
        "UNITS: seconds, because this stack is the Go SDK. On a Core-based SDK "
        "(TypeScript, Python, .NET) the same series name carries MILLISECONDS "
        "and the 0.2 threshold is wrong by 1000x. Run `make verify-sdk-labels`.\n\n"
        "Scenarios 1 and 4 look IDENTICAL on this panel. Host CPU is what "
        "separates them, and it is not a Temporal metric — use `docker stats` "
        "or node_exporter.",
        thr={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                           {"color": SERIOUS, "value": 0.2}]},
        overrides=[colour("schedule-to-start p99", C2, label="s2s p99 (seconds)"),
                   colour("slots available", C3, axis="right", unit="short",
                          label="free slots")],
        fill=10))

    text = base("text", BASE_ID + 4, "Which shape am I looking at?",
                {"h": 8, "w": 12, "x": 12, "y": y0 + 9},
                "The scenario matrix, on the board rather than in the runbook. "
                "Scenario 8 is the only row where poll success rate is the ONLY "
                "thing that moved.")
    del text["datasource"]
    text["transparent"] = True
    text["options"] = {
        "mode": "markdown",
        "code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False},
        "content": (
            "| | Sync match | Poll success | S2S P99 | Slots | Host CPU |\n"
            "|---|---|---|---|---|---|\n"
            "| Healthy | ~100% | high | ~0 | free | moderate |\n"
            "| Backlog **(1)** | ↓ | high | ↑ | 0 | **high** |\n"
            "| Slot starve **(4)** | ↓ | high | ↑ | 0 | **low** |\n"
            "| **Poller flood (8)** | **~100%** | **↓↓** | **~0** | **free** | low |\n"
            "| Blackout **(5)** | ↓↓ | — | blank | blank | — |\n\n"
            "**Scenario 8 is the only row where poll success rate is the only "
            "thing that moved.** A starved fleet and a flooded fleet both push "
            "it down and need *opposite* responses.\n\n"
            "**Host CPU is not a Temporal metric.** It comes from `docker stats` "
            "or node_exporter. Without it, scenarios 1 and 4 are "
            "indistinguishable here — same reason the runbook insists on "
            "node-level metrics in the same Grafana.\n\n"
            "Blackout shows *blank*, not zero: SDK metrics come from the "
            "Workers, so an absent Worker emits nothing at all."
        )}
    P.append(text)
    return P


def main():
    if not PATH.exists():
        sys.exit(f"dashboard not found: {PATH}\n"
                 f"Run tools/generate_golden_signals.py first.")

    d = json.loads(PATH.read_text())
    panels = d.get("panels", [])

    # Idempotency: drop everything we previously owned, then re-add. Marker,
    # not title — renaming a panel would otherwise orphan the old one.
    kept = [p for p in panels if not p.get(MARKER)]
    removed = len(panels) - len(kept)

    ours = set(range(BASE_ID, BASE_ID + 5))
    clash = sorted(ours & {p.get("id") for p in kept})
    if clash:
        # Exit rather than overwrite. Silently renumbering someone else's panel
        # breaks their alert annotations and dashboard links.
        sys.exit(f"ID collision with panels not owned by this script: {clash}\n"
                 f"Another generator has grown into the {BASE_ID}+ range. "
                 f"Raise BASE_ID here rather than renumbering theirs.")

    y0 = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in kept), default=0)
    d["panels"] = kept + build(y0)

    tags = d.setdefault("tags", [])
    if "poll-outcomes" not in tags:
        tags.append("poll-outcomes")

    PATH.write_text(json.dumps(d, indent=2) + "\n")
    action = f"replaced {removed} existing" if removed else "added"
    print(f"{action} poll-outcome panels in {PATH}")
    print(f"  row P at y={y0}, panel ids {BASE_ID}-{BASE_ID + 4}")


if __name__ == "__main__":
    main()
