#!/usr/bin/env bash
#
# Pulls the community dashboards from temporalio/dashboards and normalizes them
# so Grafana can provision them from disk.
#
# Why normalization is needed: those dashboards are published for MANUAL import
# through the Grafana UI. They carry an "__inputs" block containing datasource
# placeholders like ${DS_PROMETHEUS} that the import wizard fills in
# interactively. File provisioning has no wizard — it loads the JSON verbatim,
# the placeholder never resolves, and every panel renders "Datasource
# ${DS_PROMETHEUS} was not found".
#
# This script strips the import scaffolding and repoints every datasource
# reference at the provisioned Prometheus instance.
#
# Usage:  ./scripts/fetch-community-dashboards.sh
# Then:   docker compose restart grafana   (or wait 30s for the provider to rescan)

set -euo pipefail

REPO_URL="https://github.com/temporalio/dashboards.git"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/grafana/dashboards/community"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

command -v git >/dev/null || { echo "git is required"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

echo "==> Cloning $REPO_URL"
git clone --depth 1 "$REPO_URL" "$TMP/dashboards"

mkdir -p "$DEST"
rm -f "$DEST"/*.json

# server/ and sdk/ only.
#
# cloud/ is deliberately excluded: those dashboards query temporal_cloud_v1_*
# metrics, which do not exist on a self-hosted cluster. Provisioning them would
# add a folder of permanently empty panels, and a dashboard that is empty by
# design teaches people to ignore empty panels that are empty by accident.
#
# misc/ is excluded because the upstream README flags it as untested.
# Which SDK metrics flavour this stack actually emits.
#
#   tally  (default)  the Go SDK wired through uber-go/tally, which is what
#                     app/metrics.go does. Emits temporal_*_seconds histograms
#                     and _total counters.
#   otel              the OpenTelemetry exporter. DIFFERENT metric names —
#                     e.g. temporal_activity_schedule_to_start_latency_bucket
#                     with no _seconds suffix.
#   all               import everything and accept the dark panels.
#
# Importing the wrong flavour is not harmless. Measured on this stack, the two
# OTel dashboards render 16-18 of their panels permanently empty, because they
# query metric names a Tally exporter never produces. That is precisely the
# "empty by design teaches people to ignore empty by accident" failure the
# cloud/ exclusion below exists to avoid.
SDK_FLAVOR="${SDK_FLAVOR:-tally}"

skipped_flavor=0
skipped_format=0

for dir in server sdk; do
  if [ ! -d "$TMP/dashboards/$dir" ]; then
    echo "!! $dir/ not found upstream — repo layout may have changed, skipping"
    continue
  fi
  while IFS= read -r -d '' f; do
      base="$(basename "$f" .json)"
      lower="$(echo "$base" | tr '[:upper:]' '[:lower:]')"

      if [ "$dir" = sdk ] && [ "$SDK_FLAVOR" != all ]; then
        case "$SDK_FLAVOR:$lower" in
          tally:*otel*|otel:*tally*)
            echo "    - skipped ${dir}/${base}.json (SDK_FLAVOR=$SDK_FLAVOR)"
            skipped_flavor=$((skipped_flavor+1))
            continue
            ;;
        esac
      fi

      out="$DEST/${dir}-${base}.json"
      # `|| rc=$?` is required: under `set -e` a bare non-zero exit from the
      # normalizer would kill the whole loop, and a rejected Datadog dashboard
      # returns 3 by design.
      rc=0
      python3 "$(dirname "${BASH_SOURCE[0]}")/normalize_dashboard.py" \
        "$f" "$out" "${dir}-${base}" 2>/dev/null || rc=$?
      case $rc in
        0) echo "    + ${dir}-${base}.json" ;;
        3) echo "    - skipped ${dir}/${base}.json (not a Grafana dashboard)"
           skipped_format=$((skipped_format+1)) ;;
        *) echo "    ! skipped ${dir}/${base}.json (failed to normalize)" ;;
      esac
  done < <(find "$TMP/dashboards/$dir" -name '*.json' -print0)
done

count=$(find "$DEST" -name '*.json' | wc -l | tr -d ' ')
echo "==> Wrote $count dashboard(s) to grafana/dashboards/community/"
[ "$skipped_flavor" -gt 0 ] && \
  echo "    ($skipped_flavor skipped as wrong SDK flavour — SDK_FLAVOR=all to import them anyway)"
[ "$skipped_format" -gt 0 ] && \
  echo "    ($skipped_format skipped as not Grafana dashboards, e.g. the Datadog one)"
echo
echo "Note: these are community dashboards, not Temporal-supported ones. The"
echo "upstream README states they are not intended for production use and are"
echo "not tested against every server version."
echo
echo "Some panels will still be empty, and that is correct: many SDK metrics are"
echo "counters for failures that have not happened yet (temporal_request_failure_total,"
echo "temporal_activity_execution_failed_total, sticky cache misses). Run a chaos"
echo "scenario and they populate."
