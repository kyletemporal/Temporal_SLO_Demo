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
down: ## Stop the stack, KEEPING metrics and logs
	@# No -v. Error budgets are cumulative over 28 days and are worthless if a
	@# routine stop wipes them. `make reset` is the destructive one.
	docker compose down

.PHONY: reset
reset: ## Stop the stack and DELETE all metrics, logs and database state
	docker compose down -v

.PHONY: logs
logs: ## Tail logs for the demo app
	docker compose logs -f api worker

.PHONY: deps-refresh
deps-refresh: ## Re-resolve Go dependencies to current versions (updates go.mod/go.sum)
	@echo "==> Re-resolving the module graph against current upstream versions."
	@echo "    This can break the build — that is the point of doing it"
	@echo "    deliberately, with a diff, rather than on every customer build."
	docker run --rm -v "$(CURDIR)/app":/src -w /src golang:1.26-alpine \
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

.PHONY: verify-sdk-labels
verify-sdk-labels: ## Check the shipped alerts match YOUR SDK's metric labels
	bash scripts/verify-sdk-labels.sh

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
# Namespaces and Nexus
#
# scripts/tctl.sh wraps the temporal CLI so the address is always right —
# auto-setup binds the Frontend to the container's eth0 address, so inside the
# container 127.0.0.1 is refused. See docs/NEXUS.md.
# -----------------------------------------------------------------------------
.PHONY: nexus-doctor
nexus-doctor: ## Check Nexus end to end — the callback config registration does NOT verify
	@bash scripts/tctl.sh doctor

.PHONY: ns-list
ns-list: ## List Namespaces
	@bash scripts/tctl.sh ns list

.PHONY: ns-create
ns-create: ## Create a Namespace: make ns-create NAME=payments [RETENTION=168h]
	@test -n "$(NAME)" || { echo "usage: make ns-create NAME=<namespace> [RETENTION=168h]"; exit 2; }
	@bash scripts/tctl.sh ns create "$(NAME)" --retention "$(or $(RETENTION),72h)"

.PHONY: ns-delete
ns-delete: ## Delete a Namespace AND every history in it: make ns-delete NAME=payments
	@test -n "$(NAME)" || { echo "usage: make ns-delete NAME=<namespace>"; exit 2; }
	@bash scripts/tctl.sh ns delete "$(NAME)"

.PHONY: nexus-list
nexus-list: ## List Nexus Endpoints
	@bash scripts/tctl.sh nexus list

.PHONY: nexus-create
nexus-create: ## Create an Endpoint: make nexus-create EP=payments-api NS=payments TQ=billing
	@test -n "$(EP)" -a -n "$(NS)" -a -n "$(TQ)" || \
		{ echo "usage: make nexus-create EP=<endpoint> NS=<namespace> TQ=<task-queue>"; exit 2; }
	@bash scripts/tctl.sh nexus create "$(EP)" --namespace "$(NS)" --task-queue "$(TQ)"

.PHONY: nexus-delete
nexus-delete: ## Delete an Endpoint: make nexus-delete EP=payments-api
	@test -n "$(EP)" || { echo "usage: make nexus-delete EP=<endpoint>"; exit 2; }
	@bash scripts/tctl.sh nexus delete "$(EP)"

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
# WORKERS defaults to 5 so the documented behaviour of this target is unchanged;
# scenario 8 needs a much larger fleet and passes WORKERS=20 rather than
# duplicating the scaling logic.
scale-up: ## Scale workers up (default 5) — use mid-storm to show recovery. WORKERS=20 to override
	docker compose up -d --scale worker=$(or $(WORKERS),5) worker

.PHONY: scale-reset
scale-reset: ## Back to 1 worker
	docker compose up -d --scale worker=1 worker

# Force a REAL non-determinism error.
#
# Starts long-running Workflows, then swaps in Worker code that issues a command
# their recorded histories do not contain, so replay diverges. This is how the
# NDE alert's label name was verified rather than copied — see the comment on
# TemporalNonDeterminismError in prometheus/alerts.yml.
#
# Expect: TemporalNonDeterminismError fires, TMPRL1100 appears in the worker
# logs with WorkflowID and RunID, and the "Find stuck executions" panel names
# the affected executions.
.PHONY: chaos-stuck
chaos-stuck: ## [6] Stuck Workflows — the case no metric can see
	@echo "==> 3 PARKED workflows (await a signal that never arrives)"
	@for i in 1 2 3; do curl -sf -X POST localhost:8081/orders -H 'Content-Type: application/json' \
	  -d "{\"orderId\":\"stuck-parked-$$i\",\"stuckMode\":\"parked\",\"activityDelayMs\":50,\"maxAttempts\":3,\"wait\":false}" >/dev/null || true; done
	@echo "==> 2 RETRY-STORM workflows (unlimited retries, never fail, never finish)"
	@for i in 1 2; do curl -sf -X POST localhost:8081/orders -H 'Content-Type: application/json' \
	  -d "{\"orderId\":\"stuck-storm-$$i\",\"stuckMode\":\"retry-storm\",\"activityDelayMs\":50,\"wait\":false}" >/dev/null || true; done
	@echo
	@echo "WHAT TO EXPECT — this is the point of the scenario:"
	@echo
	@echo "  PARKED:      NOTHING fires, and nothing ever will. The Workflows are"
	@echo "               Running: pollers healthy, zero errors, zero retries,"
	@echo "               slots free. They move NO Prometheus counter, because"
	@echo "               every counter here (workflow_success / _failed /"
	@echo "               _timeout) describes a Workflow that ENDED. One that"
	@echo "               never ends increments nothing. Only DURATION is wrong,"
	@echo "               and no metric carries it — this is exactly why"
	@echo "               monitor/ polls Visibility instead."
	@echo
	@echo "  RETRY-STORM: Activity failure rate climbs, so this one IS visible."
	@echo "               But the Workflow never fails, so workflow_failed stays"
	@echo "               flat and the failure-ratio alert never fires. It burns"
	@echo "               Actions indefinitely while looking like a retry blip."
	@echo
	@echo "  NOTE: these run with NO WorkflowExecutionTimeout, unlike every other"
	@echo "  scenario here (which caps at 10m). That is deliberate. An execution"
	@echo "  timeout CONVERTS an invisible stuck Workflow into a visible timed-out"
	@echo "  one — measured: with the 10m cap these ended TimedOut and burned"
	@echo "  error budget within minutes. Setting one is a real mitigation;"
	@echo "  Temporal's default is no timeout, and this shows that exposure."
	@echo
	@echo "  Pre-existing burn alerts from earlier chaos may already be firing."
	@echo "  Check activeAt before attributing anything to this scenario."
	@echo
	@echo "Clean up:    make chaos-stuck-release"

.PHONY: chaos-stuck-release
chaos-stuck-release: ## Release parked Workflows and terminate retry storms
	@docker exec tobs-temporal sh -c 'A=$$(hostname -i):7233; \
	  temporal workflow signal --address $$A --name resume --input null \
	    --query "ExecutionStatus='"'"'Running'"'"' AND WorkflowId STARTS_WITH '"'"'order-stuck-parked'"'"'" --yes' 2>&1 | tail -1
	@docker exec tobs-temporal sh -c 'A=$$(hostname -i):7233; \
	  temporal workflow terminate --address $$A --reason "lab cleanup" \
	    --query "ExecutionStatus='"'"'Running'"'"' AND WorkflowId STARTS_WITH '"'"'order-stuck-storm'"'"'" --yes' 2>&1 | tail -1

.PHONY: chaos-nde
chaos-nde: ## [7] Non-determinism — the failure Temporal cannot retry past
	@echo "==> starting long-running workflows"
	@for i in 1 2 3; do curl -sf -X POST localhost:8081/orders -H 'Content-Type: application/json' \
	  -d "{\"orderId\":\"nde-$$i\",\"failureRate\":0,\"activityDelayMs\":45000,\"maxAttempts\":3,\"wait\":false}" >/dev/null || true; done
	@echo "==> recreating the worker with divergent code (NDE_INJECT=1)"
	@NDE_INJECT=1 docker compose up -d --force-recreate worker >/dev/null 2>&1
	@echo "    the worker service keeps its DNS name, so Prometheus still scrapes it"
	@echo "    and TemporalNonDeterminismError can actually fire"
	@echo "==> wait ~90s, then: make chaos-nde-stop"

.PHONY: chaos-nde-stop
chaos-nde-stop: ## Restore the normal Worker after chaos-nde
	@docker compose up -d --force-recreate worker >/dev/null 2>&1 && echo "normal worker restored"

# Scenario 8 is the only one that runs the fleet in the OPPOSITE direction:
# every other scenario starves it. WORKERS and DURATION are overridable —
# the effect scales with the RATIO of pollers to offered work, not with either
# number alone.
.PHONY: chaos-poller-flood
chaos-poller-flood: ## [8] Poller flood — healthy fleet, collapsing poll success rate
	bash scripts/chaos-poller-flood.sh $(or $(WORKERS),20) $(or $(DURATION),10m)

.PHONY: chaos-poller-flood-reset
chaos-poller-flood-reset: ## REQUIRED cleanup for [8] — back to 1 Worker
	bash scripts/chaos-poller-flood-reset.sh

# -----------------------------------------------------------------------------
# AI agents
#
# Everything here runs against the OFFLINE MOCK provider by default: no API key,
# no egress, no spend. That is deliberate — an observability lab whose headline
# dashboard needs a funded account is a lab most people never see working.
#
# To drive a real provider, set ANTHROPIC_API_KEY or OPENAI_API_KEY in .env and
# pass PROVIDER=anthropic (or openai).
# -----------------------------------------------------------------------------
.PHONY: agent
agent: ## Run one agent execution and print the result. PROVIDER=anthropic|openai|mock
	@curl -s -X POST localhost:8081/agent \
		-H 'content-type: application/json' \
		-d '{"prompt":"$(or $(PROMPT),Where is order order-42 and what will it cost to ship to Bristol?)","provider":"$(or $(PROVIDER),mock)","wait":true}' \
		| python3 -m json.tool

.PHONY: agent-demo
agent-demo: ## Populate every panel on the AI agent board (~20 runs, mock provider)
	@bash scripts/agent-demo.sh $(or $(RUNS),20) $(or $(PROVIDER),mock)

.PHONY: agent-runaway
agent-runaway: ## The runaway agent: loops to the cap, spends money, NO alert fires
	@echo "==> starting 5 agents that never converge"
	@for i in 1 2 3 4 5; do \
		curl -s -X POST localhost:8081/agent -H 'content-type: application/json' \
			-d "{\"prompt\":\"runaway $$i\",\"provider\":\"$(or $(PROVIDER),mock)\",\"loopForever\":true,\"maxIterations\":$(or $(MAX_ITER),8)}" >/dev/null; \
	done
	@echo "==> watch the AI agent board:"
	@echo "    Loop depth p99         climbs to the cap"
	@echo "    Run outcomes           max_iterations, NOT error"
	@echo "    Spend rate             rises"
	@echo "    Every Temporal panel   unchanged — the Workflows SUCCEEDED"

.PHONY: agent-langchain
agent-langchain: ## Start the LangChain (Python) worker — needs an API key, large image
	@if [ -z "$$ANTHROPIC_API_KEY" ] && [ -z "$$OPENAI_API_KEY" ]; then \
		echo "ERROR: set ANTHROPIC_API_KEY or OPENAI_API_KEY in demo/.env first."; \
		echo "       The LangChain worker has no offline mock — unlike the Go agent,"; \
		echo "       it would crash-loop without one and look like a broken stack."; \
		exit 1; \
	fi
	docker compose --profile langchain up -d --build agent-worker-langchain
	@echo "==> polling task queue 'agents-langchain'"
	@echo "    Its gen_ai_* series are IDENTICAL in name to the Go worker's."
	@echo "    Split them on the AI agent board with the Worker picker."

.PHONY: agent-py-test
agent-py-test: ## Verify the Python worker agrees with the Go worker (cost, taxonomy, names)
	@# Host first, image second. The OTel Python packages are NOT a prerequisite
	@# of this demo — the langchain worker is optional and behind a profile — so
	@# a bare `python3 test_parity.py` fails with ModuleNotFoundError on a
	@# perfectly good machine. Falling back to the image keeps the check
	@# runnable everywhere, at the cost of building it once.
	@if python3 -c "import opentelemetry.exporter.otlp.proto.grpc.metric_exporter" 2>/dev/null; then \
		cd agent-python && python3 test_parity.py; \
	else \
		echo "==> OTel Python packages not on this host — running in the image"; \
		docker compose --profile langchain run --rm --no-deps \
			--entrypoint python agent-worker-langchain test_parity.py; \
	fi

.PHONY: agent-test
agent-test: ## Go unit tests: cost maths, error taxonomy, loop termination
	@cd app && go test ./... 2>&1 | tail -20

# -----------------------------------------------------------------------------
# Scale and capacity
# -----------------------------------------------------------------------------
.PHONY: scale-benchmark
scale-benchmark: ## Sweep worker counts under fixed load, write a CSV + report
	@bash scripts/scale-benchmark.sh

.PHONY: scale-benchmark-quick
scale-benchmark-quick: ## Faster sweep (1,2 workers @ 2m) — shape only, not a real measurement
	@WORKERS="1 2" DURATION=2m WARMUP=60 bash scripts/scale-benchmark.sh

.PHONY: dashboards-generate
dashboards-generate: ## Regenerate every generated dashboard from tools/
	@cd .. && for g in tools/generate_*.py; do python3 "$$g"; done
	@echo "==> restart Grafana to reload: docker compose restart grafana"

.PHONY: dashboards-check
dashboards-check: ## Run every dashboard query against live Prometheus, report empties
	@cd .. && for d in demo/grafana/dashboards/custom/*.json demo/grafana/dashboards/slo/*.json; do \
		python3 tools/check_dashboard_queries.py "$$d" || true; echo; \
	done

# -----------------------------------------------------------------------------
# Worker tuning lab — live UI over real Workers (see lab/README.md)
# -----------------------------------------------------------------------------
.PHONY: lab
lab: ## Start the Worker tuning lab UI at http://localhost:8095 (Go + Python Workers)
	docker compose --profile lab up -d --build lab-controller lab-worker-go lab-worker-python
	@echo "==> Worker tuning lab: http://localhost:8095  (starts idle — press Start load)"

.PHONY: lab-down
lab-down: ## Stop the tuning lab and terminate any lab Workflows still running
	-docker compose --profile lab stop lab-controller lab-worker-go lab-worker-python
	@docker exec tobs-temporal sh -c 'export TEMPORAL_ADDRESS=$$(hostname -i | cut -d" " -f1):7233; \
	  temporal workflow terminate --namespace default \
	    --query "TaskQueue=\"lab-go\" OR TaskQueue=\"lab-python\"" \
	    --reason "make lab-down" --yes' 2>/dev/null || true
