#!/usr/bin/env python3
"""Rewrite a manually-importable Grafana dashboard for file provisioning.

Three transformations, each fixing a specific failure mode:

1. Drop __inputs / __requires.
   These drive the interactive import wizard. Left in place, Grafana's file
   provisioner either rejects the dashboard or loads it with unresolved
   placeholders.

2. Rewrite ${DS_*} datasource placeholders to the provisioned UID.
   Handles both the modern object form {"type": "prometheus", "uid": "${DS_X}"}
   and the legacy string form "datasource": "${DS_X}".

   Dashboard-local template variables (e.g. ${datasource}, ${DS} defined under
   "templating") are left alone — those resolve at render time and rewriting
   them would break the variable dropdown.

3. Clear "id" and set a deterministic "uid".
   A stale numeric id collides with whatever already occupies that slot;
   a missing uid makes every provisioner restart create a duplicate.

Usage: normalize_dashboard.py <input.json> <output.json> <uid-slug>
"""

import json
import re
import sys

# Import-wizard placeholders look like ${DS_PROMETHEUS} or ${DS_LOKI}.
# Deliberately anchored and uppercase-only so it cannot match a real
# user-defined variable such as ${datasource}.
DS_PLACEHOLDER = re.compile(r"^\$\{DS_[A-Z0-9_]+\}$")

TARGET_UID = "prometheus"


def rewrite(node):
    """Walk the tree, replacing datasource placeholders in place."""
    if isinstance(node, dict):
        return {k: rewrite(v) for k, v in node.items()}
    if isinstance(node, list):
        return [rewrite(v) for v in node]
    if isinstance(node, str) and DS_PLACEHOLDER.match(node):
        return TARGET_UID
    return node


def main():
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2

    src, dst, uid_slug = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        with open(src, encoding="utf-8") as fh:
            dash = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {src}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(dash, dict):
        print(f"{src}: top level is not a JSON object", file=sys.stderr)
        return 1

    # Not every .json in the upstream repo is a Grafana dashboard. The sdk/
    # directory also ships a DATADOG dashboard, which uses "widgets" instead of
    # "panels". Grafana provisions it without complaint and renders a completely
    # blank dashboard — no error, no panels, nothing to click. Detecting it here
    # is the difference between "we exclude that one, here is why" and a
    # customer opening an empty page and concluding the stack is broken.
    if "widgets" in dash or "layout_type" in dash:
        print(f"{src}: Datadog dashboard (has 'widgets'), not Grafana", file=sys.stderr)
        return 3

    if not isinstance(dash.get("panels"), list):
        print(f"{src}: no 'panels' array — not a Grafana dashboard", file=sys.stderr)
        return 3

    dash.pop("__inputs", None)
    dash.pop("__requires", None)

    dash = rewrite(dash)

    dash["id"] = None
    # Grafana caps uid length at 40 characters.
    dash["uid"] = re.sub(r"[^a-zA-Z0-9-]", "-", uid_slug)[:40]

    title = dash.get("title") or uid_slug
    dash["title"] = f"{title} (community)"

    tags = dash.get("tags") or []
    if "community" not in tags:
        tags.append("community")
    dash["tags"] = tags

    try:
        with open(dst, "w", encoding="utf-8") as fh:
            json.dump(dash, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        print(f"cannot write {dst}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
