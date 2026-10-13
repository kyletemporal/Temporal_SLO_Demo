#!/usr/bin/env bash
# Layer 2 of 2: install Temporal and its monitoring onto the cluster that
# ../terraform created. Safe to re-run; every step is idempotent.
#
#   cd aws/eks-selfhosted/terraform && terraform apply
#   ../scripts/deploy.sh
#   ../scripts/smoke-test.sh
#
# Inputs come from `terraform output` by default. Every one can be overridden
# by environment variable, and setting DB_HOST switches to "env" mode, which
# skips AWS entirely — that is how this script is tested against a local
# cluster (see ../README.md, "How this was verified").
#
# Environment:
#   NUM_HISTORY_SHARDS   default 512. Permanent after the first deploy; this
#                        script refuses to change it on an existing cluster.
#   STORAGE_CLASS        PVC storage class for Prometheus. Default gp3 on EKS
#                        (created here), the cluster default in env mode.
#   VALUES_OVERRIDE      extra Helm values file layered over values-temporal.yaml
#   SKIP_MONITORING=1    install Temporal only
#   env mode:            DB_HOST DB_PORT DB_CA_FILE DB_MASTER_USER DB_MASTER_PASSWORD
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
TF_DIR="${TF_DIR:-$ROOT/terraform}"

TEMPORAL_CHART_VERSION="0.67.0"            # appVersion 1.28.1; images pinned to 1.28.4 in values
KPS_CHART_VERSION="92.3.1"
TEMPORAL_REPO="https://go.temporal.io/helm-charts"
KPS_REPO="https://prometheus-community.github.io/helm-charts"
RDS_CA_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

NS="temporal"
MON_NS="monitoring"
NUM_HISTORY_SHARDS="${NUM_HISTORY_SHARDS:-512}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "$1 is required"; }

need kubectl; need helm; need python3; need openssl

# -----------------------------------------------------------------------------
# Inputs
# -----------------------------------------------------------------------------
if [ -n "${DB_HOST:-}" ]; then
  MODE="env"
  : "${DB_PORT:=5432}"
  : "${DB_CA_FILE:?env mode needs DB_CA_FILE}"
  : "${DB_MASTER_USER:?env mode needs DB_MASTER_USER}"
  : "${DB_MASTER_PASSWORD:?env mode needs DB_MASTER_PASSWORD}"
  : "${CLUSTER_LABEL:=$(kubectl config current-context)}"
  : "${STORAGE_CLASS:=}"
else
  MODE="terraform"
  need aws; need terraform
  say "reading terraform outputs from $TF_DIR"
  TF_JSON="$(terraform -chdir="$TF_DIR" output -json)" || die "no terraform outputs — run terraform apply in $TF_DIR first"
  tf() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1']['value'])" <<<"$TF_JSON"; }
  REGION="$(tf region)"
  CLUSTER_NAME="$(tf cluster_name)"
  DB_HOST="$(tf db_host)"
  DB_PORT="$(tf db_port)"
  DB_MASTER_SECRET_ARN="$(tf db_master_secret_arn)"
  DB_APP_SECRET_ARN="$(tf db_app_secret_arn)"
  : "${CLUSTER_LABEL:=$CLUSTER_NAME}"
  : "${STORAGE_CLASS:=gp3}"

  say "pointing kubectl at $CLUSTER_NAME"
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME" >/dev/null
fi

echo "mode=$MODE  db=$DB_HOST:$DB_PORT  shards=$NUM_HISTORY_SHARDS  context=$(kubectl config current-context)"

# -----------------------------------------------------------------------------
# Shard guard — before anything is changed.
#
# numHistoryShards is fixed when the cluster first starts. If a release exists,
# its rendered config holds the value it was born with; refuse to deploy a
# different one rather than let it look like it took effect.
# -----------------------------------------------------------------------------
existing_shards="$(kubectl -n "$NS" get configmap temporal-config \
  -o jsonpath='{.data.config_template\.yaml}' 2>/dev/null \
  | sed -n 's/^ *numHistoryShards: *\([0-9]*\).*/\1/p' || true)"
if [ -n "$existing_shards" ] && [ "$existing_shards" != "$NUM_HISTORY_SHARDS" ]; then
  die "this cluster was created with numHistoryShards=$existing_shards and it cannot change.
       Re-run with NUM_HISTORY_SHARDS=$existing_shards, or build a new cluster and database."
fi

# -----------------------------------------------------------------------------
# Namespaces, storage class, CA bundle
# -----------------------------------------------------------------------------
say "namespaces"
for n in "$NS" "$MON_NS"; do
  kubectl create namespace "$n" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done

if [ "$MODE" = terraform ]; then
  say "gp3 StorageClass (EKS ships only gp2, and as the default)"
  kubectl apply -f - >/dev/null <<'EOF'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: gp3
provisioner: ebs.csi.aws.com
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
reclaimPolicy: Delete
parameters:
  type: gp3
  encrypted: "true"
EOF
fi

say "database CA bundle"
CA_TMP="$(mktemp)"; trap 'rm -f "$CA_TMP"' EXIT
if [ "$MODE" = terraform ]; then
  curl -fsSL "$RDS_CA_URL" -o "$CA_TMP" || die "could not download the RDS CA bundle from $RDS_CA_URL"
else
  cp "$DB_CA_FILE" "$CA_TMP"
fi
grep -q "BEGIN CERTIFICATE" "$CA_TMP" || die "CA bundle does not look like PEM"
kubectl -n "$NS" create configmap rds-ca --from-file=global-bundle.pem="$CA_TMP" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# -----------------------------------------------------------------------------
# Credentials
# -----------------------------------------------------------------------------
gen_password() { openssl rand -base64 48 | tr -d '/+=\n' | cut -c1-32; }

say "temporal role password"
if [ "$MODE" = terraform ]; then
  if APP_PASSWORD="$(aws secretsmanager get-secret-value --region "$REGION" \
        --secret-id "$DB_APP_SECRET_ARN" --query SecretString --output text 2>/dev/null)"; then
    echo "using existing value from Secrets Manager"
  else
    APP_PASSWORD="$(gen_password)"
    aws secretsmanager put-secret-value --region "$REGION" --secret-id "$DB_APP_SECRET_ARN" \
      --secret-string "$APP_PASSWORD" >/dev/null
    echo "generated and stored in Secrets Manager"
  fi
  MASTER_JSON="$(aws secretsmanager get-secret-value --region "$REGION" \
    --secret-id "$DB_MASTER_SECRET_ARN" --query SecretString --output text)"
  DB_MASTER_USER="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["username"])' <<<"$MASTER_JSON")"
  DB_MASTER_PASSWORD="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])' <<<"$MASTER_JSON")"
  unset MASTER_JSON
else
  APP_PASSWORD="$(kubectl -n "$NS" get secret temporal-db -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || true)"
  [ -n "$APP_PASSWORD" ] && echo "reusing existing Kubernetes secret" || { APP_PASSWORD="$(gen_password)"; echo "generated"; }
fi
kubectl -n "$NS" create secret generic temporal-db --from-literal=password="$APP_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# -----------------------------------------------------------------------------
# Database bootstrap: role + databases, as the master user, once per deploy.
# The master credential exists in the cluster only for the life of this Job.
# -----------------------------------------------------------------------------
say "bootstrapping the temporal role and databases"
kubectl -n "$NS" delete job db-bootstrap --ignore-not-found >/dev/null
kubectl -n "$NS" create secret generic db-bootstrap \
  --from-literal=username="$DB_MASTER_USER" --from-literal=password="$DB_MASTER_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
unset DB_MASTER_PASSWORD
kubectl -n "$NS" create configmap db-bootstrap-sql --from-file="$ROOT/helm/db-bootstrap.sql" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: db-bootstrap
spec:
  backoffLimit: 6
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: OnFailure
      securityContext: { runAsNonRoot: true, runAsUser: 70 }
      containers:
        - name: psql
          image: postgres:16-alpine
          command: ["sh", "-c", 'exec psql -v app_password="\$APP_PASSWORD" -f /sql/db-bootstrap.sql']
          env:
            - { name: PGHOST, value: "$DB_HOST" }
            - { name: PGPORT, value: "$DB_PORT" }
            - { name: PGDATABASE, value: postgres }
            - { name: PGSSLMODE, value: verify-full }
            - { name: PGSSLROOTCERT, value: /ca/global-bundle.pem }
            - { name: PGUSER, valueFrom: { secretKeyRef: { name: db-bootstrap, key: username } } }
            - { name: PGPASSWORD, valueFrom: { secretKeyRef: { name: db-bootstrap, key: password } } }
            - { name: APP_PASSWORD, valueFrom: { secretKeyRef: { name: temporal-db, key: password } } }
          volumeMounts:
            - { name: sql, mountPath: /sql }
            - { name: ca, mountPath: /ca }
      volumes:
        - { name: sql, configMap: { name: db-bootstrap-sql } }
        - { name: ca, configMap: { name: rds-ca } }
EOF
if ! kubectl -n "$NS" wait --for=condition=complete job/db-bootstrap --timeout=5m; then
  kubectl -n "$NS" logs job/db-bootstrap --tail=30 || true
  kubectl -n "$NS" delete secret db-bootstrap --ignore-not-found >/dev/null
  die "database bootstrap failed (logs above)"
fi
kubectl -n "$NS" logs job/db-bootstrap --tail=20 | sed 's/^/    /'
kubectl -n "$NS" delete secret db-bootstrap >/dev/null
echo "master credential removed from the cluster"

# -----------------------------------------------------------------------------
# Monitoring first: Temporal's ServiceMonitor needs the CRDs it installs.
# -----------------------------------------------------------------------------
if [ "${SKIP_MONITORING:-0}" != 1 ]; then
  say "kube-prometheus-stack $KPS_CHART_VERSION"
  if ! kubectl -n "$MON_NS" get secret grafana-admin >/dev/null 2>&1; then
    kubectl -n "$MON_NS" create secret generic grafana-admin \
      --from-literal=admin-user=admin --from-literal=admin-password="$(gen_password)" >/dev/null
  fi
  sc_args=()
  if [ -n "$STORAGE_CLASS" ]; then
    sc_args=(--set "prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName=$STORAGE_CLASS")
  fi
  helm upgrade --install monitoring kube-prometheus-stack \
    --repo "$KPS_REPO" --version "$KPS_CHART_VERSION" \
    -n "$MON_NS" -f "$ROOT/helm/values-monitoring.yaml" \
    --set "prometheus.prometheusSpec.externalLabels.cluster=$CLUSTER_LABEL" \
    ${sc_args[@]+"${sc_args[@]}"} --wait --timeout 10m

  say "production/ rules as PrometheusRules (unchanged)"
  for f in slo-rules alerts visibility-rules; do
    src="$REPO/production/prometheus/$f.yml"
    {
      printf 'apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n'
      printf '  name: temporal-production-%s\n  namespace: %s\n' "$f" "$MON_NS"
      printf '  labels: { app.kubernetes.io/part-of: temporal-selfhosted }\nspec:\n'
      sed 's/^/  /' "$src"
    } | kubectl apply --server-side -f - >/dev/null
    echo "  $f.yml"
  done

  say "production/ dashboards"
  for d in "$REPO"/production/grafana/dashboards/*.json; do
    name="temporal-prod-$(basename "$d" .json)"
    kubectl -n "$MON_NS" create configmap "$name" --from-file="$d" --dry-run=client -o yaml \
      | kubectl label --local -f - grafana_dashboard=1 -o yaml \
      | kubectl annotate --local -f - grafana_folder="Temporal (production)" -o yaml \
      | kubectl apply --server-side -f - >/dev/null
    echo "  $(basename "$d")"
  done
fi

# -----------------------------------------------------------------------------
# Temporal
# -----------------------------------------------------------------------------
say "temporal chart $TEMPORAL_CHART_VERSION (server 1.28.4)"
values=(-f "$ROOT/helm/values-temporal.yaml")
[ -n "${VALUES_OVERRIDE:-}" ] && values+=(-f "$VALUES_OVERRIDE")
helm upgrade --install temporal temporal \
  --repo "$TEMPORAL_REPO" --version "$TEMPORAL_CHART_VERSION" \
  -n "$NS" "${values[@]}" \
  --set "server.config.numHistoryShards=$NUM_HISTORY_SHARDS" \
  --set "server.config.persistence.default.sql.host=$DB_HOST" \
  --set "server.config.persistence.default.sql.port=$DB_PORT" \
  --set "server.config.persistence.default.sql.tls.serverName=$DB_HOST" \
  --set "server.config.persistence.visibility.sql.host=$DB_HOST" \
  --set "server.config.persistence.visibility.sql.port=$DB_PORT" \
  --set "server.config.persistence.visibility.sql.tls.serverName=$DB_HOST"

say "waiting for the schema job"
rev="$(helm -n "$NS" status temporal -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
if ! kubectl -n "$NS" wait --for=condition=complete "job/temporal-schema-$rev" --timeout=10m; then
  for c in setup-default-store setup-visibility-store update-default-store update-visibility-store; do
    kubectl -n "$NS" logs "job/temporal-schema-$rev" -c "$c" --tail=15 2>/dev/null | sed "s/^/  [$c] /"
  done
  die "schema job did not complete"
fi

say "waiting for server roles"
for d in frontend history matching worker web admintools; do
  kubectl -n "$NS" rollout status "deployment/temporal-$d" --timeout=10m
done

if [ "${SKIP_MONITORING:-0}" != 1 ]; then
  kubectl apply -f "$ROOT/helm/servicemonitor.yaml" >/dev/null
fi

say "namespace 'default'"
kubectl -n "$NS" exec deploy/temporal-admintools -- sh -c \
  'temporal operator namespace describe -n default >/dev/null 2>&1 \
   || temporal operator namespace create -n default --retention 72h'

say "done"
cat <<EOF
Temporal 1.28.4 is running in namespace '$NS' with $NUM_HISTORY_SHARDS history shards.

  Smoke test : $HERE/smoke-test.sh
  Temporal UI: kubectl -n $NS port-forward svc/temporal-web 8080:8080
  Frontend   : temporal-frontend.$NS.svc:7233 (in-cluster)
               kubectl -n $NS port-forward svc/temporal-frontend 7233:7233
EOF
if [ "${SKIP_MONITORING:-0}" != 1 ]; then
cat <<EOF
  Grafana    : kubectl -n $MON_NS port-forward svc/monitoring-grafana 3000:80
               user admin, password:
               kubectl -n $MON_NS get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d
EOF
fi
