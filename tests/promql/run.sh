#!/usr/bin/env bash
# Unit-test the alerting and SLO rules with promtool (via Docker, pinned to the
# Prometheus version the stack runs). Exits non-zero on any failure.
#
#   tests/promql/run.sh            # every *.test.yml
#   tests/promql/run.sh cloud      # just tests/promql/cloud.test.yml
set -euo pipefail
cd "$(dirname "$0")/../.."
image="prom/prometheus:${PROMETHEUS_VERSION:-v3.1.0}"
if [ $# -gt 0 ]; then files=("$@"); files=("${files[@]/#/tests/promql/}"); files=("${files[@]/%/.test.yml}")
else files=(tests/promql/*.test.yml); fi
docker run --rm -v "$PWD:/w" -w /w --entrypoint promtool "$image" test rules "${files[@]}"
