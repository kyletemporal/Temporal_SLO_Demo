.DEFAULT_GOAL := help
SHELL := /bin/bash

K6 := docker compose --profile tools run --rm k6 run

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# -----------------------------------------------------------------------------
# Stack lifecycle
# -----------------------------------------------------------------------------
.PHONY: up
up: ## Build and start the full stack
	docker compose up -d --build
	@echo ""
	@echo "  Grafana      http://localhost:3000  (anonymous viewer, admin/admin to edit)"
	@echo "  Prometheus   http://localhost:9090"
	@echo "  Temporal UI  http://localhost:8080"
	@echo "  Demo API     http://localhost:8081"
	@echo ""
	@echo "  Next: make verify"

.PHONY: down
down: ## Stop the stack and remove volumes
	docker compose down -v

.PHONY: logs
logs: ## Tail logs for the demo app
	docker compose logs -f api worker

.PHONY: deps-refresh
deps-refresh: ## Re-resolve Go dependencies to current versions (updates go.mod/go.sum)
	@echo "==> Re-resolving the module graph against current upstream versions."
	@echo "    This can break the build — that is the point of doing it"
	@echo "    deliberately, with a diff, rather than on every customer build."
	docker run --rm -v "$(CURDIR)/app":/src -w /src golang:1.25-alpine \
		sh -c 'apk add --no-cache git >/dev/null && go get -u ./... && go mod tidy'
	docker compose build --no-cache api worker

.PHONY: dashboards
dashboards: ## Fetch + normalize community dashboards from temporalio/dashboards
	bash scripts/fetch-community-dashboards.sh
	docker compose restart grafana

# -----------------------------------------------------------------------------
# Verification — run this before demoing to anyone
# -----------------------------------------------------------------------------
.PHONY: verify
verify: ## Check that metrics are actually flowing end to end
	@echo "==> Prometheus targets"
	@curl -sf 'http://localhost:9090/api/v1/targets?state=active' | \
		python3 -c "import json,sys; \
		ts=json.load(sys.stdin)['data']['activeTargets']; \
		[print(f\"    {t['labels'].get('job'):24s} {t['health']:8s} {t['scrapeUrl']}\") for t in ts]" \
		|| echo "    !! Prometheus not reachable"
	@echo ""
	@echo "==> Cluster metrics present (expect a non-empty result)"
	@curl -sf 'http://localhost:9090/api/v1/query?query=count(service_requests)' | \
		python3 -c "import json,sys; r=json.load(sys.stdin)['data']['result']; \
		print('    service_requests:', r[0]['value'][1] if r else 'MISSING — check PROMETHEUS_ENDPOINT on the temporal service')"
	@echo ""
	@echo "==> SDK metrics present (expect a non-empty result)"
	@curl -sf 'http://localhost:9090/api/v1/query?query=count(temporal_worker_task_slots_available)' | \
		python3 -c "import json,sys; r=json.load(sys.stdin)['data']['result']; \
		print('    task_slots_available:', r[0]['value'][1] if r else 'MISSING — the Worker metrics exporter is not running')"
	@echo ""
	@echo "==> Sync match rate metric present (verifies the panel will render)"
	@echo "    (counter — does not exist until traffic has produced a sync match)"
	@curl -sf 'http://localhost:9090/api/v1/query?query=count(poll_success_sync)' | \
		python3 -c "import json,sys; r=json.load(sys.stdin)['data']['result']; \
		print('    poll_success_sync:', r[0]['value'][1] if r else 'not yet emitted — run \`make smoke\`, wait ~15s, re-run \`make verify\`.\n      Still missing after traffic? The name varies by server version; see SETUP.md.')"

.PHONY: validate
validate: ## Full end-to-end check: containers, targets, rules, every panel, SLOs
	bash scripts/validate.sh

.PHONY: smoke
smoke: ## Start a single order to confirm the app works
	curl -sf -X POST localhost:8081/orders \
		-H 'Content-Type: application/json' \
		-d '{"orderId":"smoke-1","failureRate":0,"activityDelayMs":10,"maxAttempts":1,"wait":true}' | \
		python3 -m json.tool

# -----------------------------------------------------------------------------
# Chaos scenarios
# -----------------------------------------------------------------------------
.PHONY: baseline
baseline: ## [0] Healthy steady state — run this FIRST, for 10m
	$(K6) /scripts/00-baseline.js

.PHONY: chaos-backlog
chaos-backlog: ## [1] Backlog storm — demand exceeds Worker capacity
	$(K6) /scripts/01-backlog-storm.js

.PHONY: chaos-failures
chaos-failures: ## [2] Failure injection — absorbed vs terminal failures
	$(K6) /scripts/02-failure-injection.js

.PHONY: chaos-orphan
chaos-orphan: ## [3] Orphaned Task Queue — work sent where nobody listens
	$(K6) /scripts/03-orphan-queue.js

.PHONY: chaos-slots
chaos-slots: ## [4] Slot saturation — starved Worker on idle hardware
	$(K6) /scripts/04-slot-saturation.js

.PHONY: chaos-blackout
chaos-blackout: ## [5] Worker blackout and recovery
	bash scripts/chaos-worker-blackout.sh

# -----------------------------------------------------------------------------
# Scaling helpers (used during scenarios 1 and 4)
# -----------------------------------------------------------------------------
.PHONY: scale-up
scale-up: ## Scale to 5 workers — use mid-storm to show recovery
	docker compose up -d --scale worker=5 worker

.PHONY: scale-reset
scale-reset: ## Back to 1 worker
	docker compose up -d --scale worker=1 worker
