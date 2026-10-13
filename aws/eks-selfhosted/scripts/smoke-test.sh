#!/usr/bin/env bash
# Run smoke/smoke.py inside the cluster: start a Workflow, run an Activity on a
# worker in the same pod, and confirm the closed execution reaches visibility.
#
# Needs egress to PyPI (through the VPC's NAT gateway) to install the SDK.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
NS="temporal"
SDK_VERSION="1.34.0"

kubectl -n "$NS" delete job temporal-smoke --ignore-not-found >/dev/null
kubectl -n "$NS" create configmap temporal-smoke --from-file="$ROOT/smoke/smoke.py" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: temporal-smoke
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: smoke
          image: python:3.12-slim
          command: ["sh", "-c", "pip install --quiet --disable-pip-version-check temporalio==$SDK_VERSION && python /smoke/smoke.py"]
          env:
            - { name: TEMPORAL_ADDRESS, value: "temporal-frontend.$NS.svc:7233" }
            - { name: TEMPORAL_NAMESPACE, value: default }
          volumeMounts:
            - { name: smoke, mountPath: /smoke }
      volumes:
        - { name: smoke, configMap: { name: temporal-smoke } }
EOF

echo "==> smoke test running (installs the Python SDK first; ~1 minute)"
set +e
kubectl -n "$NS" wait --for=condition=complete job/temporal-smoke --timeout=5m >/dev/null 2>&1
ok=$?
set -e
kubectl -n "$NS" logs job/temporal-smoke
if [ $ok -ne 0 ]; then
  echo "SMOKE TEST FAILED" >&2
  exit 1
fi
