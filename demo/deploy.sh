#!/usr/bin/env bash
#
# One-shot deploy: prerequisites -> build -> start -> wait -> seed -> validate.
#
#   ./deploy.sh              deploy and validate
#   ./deploy.sh --clean      tear down (including volumes) first
#   ./deploy.sh --no-seed    skip the traffic seed (SLO board will read "no traffic")
#
# Safe to re-run. Exits non-zero if the stack does not come up healthy.

set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLEAN=0
SEED=1
for arg in "$@"; do
  case "$arg" in
    --clean)   CLEAN=1 ;;
    --no-seed) SEED=0 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)"; exit 2 ;;
  esac
done

say()  { printf "\n\033[1m==> %s\033[0m\n" "$1"; }
ok()   { printf "    \033[32mok\033[0m    %s\n" "$1"; }
die()  { printf "    \033[31mfail\033[0m  %s\n" "$1"; exit 1; }

# -----------------------------------------------------------------------------
say "Checking prerequisites"
# -----------------------------------------------------------------------------
command -v docker >/dev/null || die "docker not found"
docker info >/dev/null 2>&1  || die "docker daemon not running — start Docker Desktop"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
command -v python3 >/dev/null || die "python3 not found (used by verify/validate)"
ok "docker $(docker version --format '{{.Server.Version}}'), compose $(docker compose version --short)"

# Ports must be free, unless it is our own stack already holding them.
#
# lsof is absent from many Linux images, and `lsof … >/dev/null 2>&1` exits
# non-zero when the BINARY IS MISSING exactly as it does when the port is free.
# Without this fallback the whole loop silently passes on such a host and a real
# conflict only surfaces later as a confusing compose error.
port_in_use() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnH "sport = :$1" 2>/dev/null | grep -q .
  elif command -v netstat >/dev/null 2>&1; then
    netstat -an 2>/dev/null | grep -qE "[:.]$1[[:space:]].*LISTEN"
  else
    return 1
  fi
}
if ! command -v lsof >/dev/null 2>&1 && ! command -v ss >/dev/null 2>&1 \
   && ! command -v netstat >/dev/null 2>&1; then
  printf "    \033[33mwarn\033[0m  no lsof/ss/netstat — skipping port check\n"
fi

for p in 3000 7233 8000 8080 8081 9090; do
  if port_in_use "$p"; then
    holder=$(docker ps --format '{{.Names}}\t{{.Ports}}' | grep ":$p->" | cut -f1 | head -1)
    if [ -n "$holder" ]; then
      ok "port $p held by $holder (this stack)"
    else
      die "port $p is in use by something else — free it or remap it in docker-compose.yml"
    fi
  fi
done
ok "required ports available"

# Executable bits are commonly lost by tar/zip extraction.
chmod +x scripts/*.sh scripts/*.py 2>/dev/null || true

# -----------------------------------------------------------------------------
if [ "$CLEAN" = 1 ]; then
  say "Tearing down existing stack (including volumes)"
  docker compose down -v --remove-orphans 2>&1 | tail -3
  ok "clean slate"
fi

# -----------------------------------------------------------------------------
say "Building and starting the stack"
# -----------------------------------------------------------------------------
echo "    First build pulls images and Go modules; allow a few minutes."
if ! docker compose up -d --build; then
  echo
  echo "    Build failed. Two failures account for almost all of them:"
  echo "      'toolchain upgrade needed'  -> bump FROM golang:1.25-alpine in app/Dockerfile"
  echo "      'ambiguous import genproto' -> app/metrics.go is importing the m3db fork"
  echo "    See SETUP.md > Troubleshooting."
  exit 1
fi
ok "containers started"

# -----------------------------------------------------------------------------
say "Waiting for services"
# -----------------------------------------------------------------------------
# Temporal binds the Frontend to the container IP, not loopback — the compose
# health check accounts for that. If this times out, read its health log:
#   docker inspect tobs-temporal --format '{{json .State.Health}}'
wait_for() {
  local label="$1" deadline="$2"; shift 2
  local elapsed=0
  until "$@" >/dev/null 2>&1; do
    if [ "$elapsed" -ge "$deadline" ]; then
      printf "\r"; die "$label did not become ready within ${deadline}s"
    fi
    printf "\r    ...   waiting for %s (%ss)" "$label" "$elapsed"
    sleep 3; elapsed=$((elapsed+3))
  done
  printf "\r\033[K"; ok "$label ready"
}

temporal_healthy() {
  [ "$(docker inspect -f '{{.State.Health.Status}}' tobs-temporal 2>/dev/null)" = healthy ]
}
wait_for "Temporal Service"  180 temporal_healthy
wait_for "demo API"           90 curl -sf http://localhost:8081/healthz
wait_for "Prometheus"         90 curl -sf http://localhost:9090/-/healthy
wait_for "Grafana"           120 curl -sf http://localhost:3000/api/health

# Worker replicas are found through Compose DNS on a 10s refresh, so the
# target can legitimately be missing for a few seconds after startup.
worker_target_up() {
  curl -sf 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null \
    | grep -q '"job":"temporal-sdk-worker".*"health":"up"' \
    || curl -sf 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null \
       | python3 -c "
import json,sys
ts=json.load(sys.stdin)['data']['activeTargets']
sys.exit(0 if any(t['labels'].get('job')=='temporal-sdk-worker' and t['health']=='up' for t in ts) else 1)"
}
wait_for "Worker scrape target" 90 worker_target_up

# -----------------------------------------------------------------------------
if [ "$SEED" = 1 ]; then
  say "Seeding traffic"
  # Not cosmetic. Many Temporal metrics are COUNTERS that do not exist until the
  # event they count has happened — poll_success_sync is the usual casualty, and
  # an unseeded stack reports it missing as though the server version were wrong.
  # The SLO board likewise shows "no traffic" until requests have flowed.
  if curl -sf -X POST localhost:8081/orders \
       -H 'Content-Type: application/json' \
       -d '{"orderId":"deploy-smoke","failureRate":0,"activityDelayMs":10,"maxAttempts":1,"wait":true}' \
       | grep -q '"result": *"completed"'; then
    ok "smoke Workflow completed end to end"
  else
    die "smoke Workflow did not complete — check 'docker compose logs api worker'"
  fi

  echo "    Running 90s of baseline load so the SLO board has something to measure."
  docker compose --profile tools run --rm -e DURATION=90s k6 run /scripts/00-baseline.js >/dev/null 2>&1 \
    && ok "baseline load complete" \
    || echo "    (load driver failed — stack is still up; try 'make baseline' manually)"

  echo "    Letting recording rules evaluate..."
  sleep 35
fi

# -----------------------------------------------------------------------------
say "Validating"
# -----------------------------------------------------------------------------
if bash scripts/validate.sh; then
  cat <<'EOF'

    Next:
      make baseline        10 minutes of healthy traffic (do this before any chaos)
      make chaos-slots     watch an error budget drain on the SLO board
      make validate        re-run these checks any time
      make help            everything else
EOF
  exit 0
else
  echo
  echo "    Stack is up but validation found problems. See SETUP.md > Troubleshooting."
  exit 1
fi
