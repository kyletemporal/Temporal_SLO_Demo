package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"go.temporal.io/sdk/client"
	sdktally "go.temporal.io/sdk/contrib/tally"
	"go.temporal.io/sdk/interceptor"
)

type startRequest struct {
	OrderInput
	// TaskQueue overrides the default. Point it at a queue nobody polls to
	// drive the "Tasks With No Poller" panel.
	TaskQueue string `json:"taskQueue"`
	// Wait blocks until the Workflow completes. Off by default so k6 can
	// generate backlog faster than Workers can drain it.
	Wait bool `json:"wait"`
}

type agentRequest struct {
	AgentInput
	TaskQueue string `json:"taskQueue"`
	RunID     string `json:"runId"`
	Wait      bool   `json:"wait"`
}

type agentResponse struct {
	WorkflowID string       `json:"workflowId"`
	RunID      string       `json:"runId"`
	TaskQueue  string       `json:"taskQueue"`
	Result     *AgentResult `json:"result,omitempty"`
	Error      string       `json:"error,omitempty"`
}

type startResponse struct {
	WorkflowID string `json:"workflowId"`
	RunID      string `json:"runId"`
	TaskQueue  string `json:"taskQueue"`
	Result     string `json:"result,omitempty"`
	Error      string `json:"error,omitempty"`
}

func runAPI() {
	scope := newPrometheusScope("0.0.0.0:" + env("METRICS_PORT", "8077"))
	startPprofServer("0.0.0.0:" + env("PPROF_PORT", "6060"))

	// The client side matters as much as the Worker: StartWorkflowExecution is
	// where the trace begins. Without the interceptor here, every Workflow trace
	// is rootless and you cannot see the gap between "client asked" and "Worker
	// started" — which is schedule-to-start, the first thing worth looking at.
	var apiInterceptors []interceptor.ClientInterceptor
	if ti, err := tracingInterceptor(); err != nil {
		log.Printf("WARN  tracing interceptor unavailable: %v", err)
	} else {
		apiInterceptors = append(apiInterceptors, ti)
	}

	c, err := dialTemporal(client.Options{
		HostPort:       env("TEMPORAL_ADDRESS", "localhost:7233"),
		Namespace:      env("TEMPORAL_NAMESPACE", "default"),
		MetricsHandler: sdktally.NewMetricsHandler(scope),
		Interceptors:   apiInterceptors,
	})
	if err != nil {
		log.Fatalln("unable to connect to Temporal:", err)
	}
	defer c.Close()

	defaultQueue := env("TASK_QUEUE", defaultTaskQueue)
	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintln(w, "ok")
	})

	mux.HandleFunc("/orders", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}

		var req startRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
			return
		}

		queue := req.TaskQueue
		if queue == "" {
			queue = defaultQueue
		}
		if req.OrderID == "" {
			req.OrderID = fmt.Sprintf("%d", time.Now().UnixNano())
		}

		opts := client.StartWorkflowOptions{
			ID:        workflowIDPrefix + req.OrderID,
			TaskQueue: queue,
			// Bounded so an orphaned-queue run cleans itself up instead of
			// leaving Workflows open forever after the demo.
			WorkflowExecutionTimeout: 10 * time.Minute,
		}

		// StuckMode runs with NO execution timeout, and that is the whole point.
		//
		// With the 10-minute cap above, a "stuck" Workflow is invisible for ten
		// minutes and then TIMES OUT — which increments workflow_timeout, counts
		// as bad in the workflow_completion SLI, burns error budget, and fires
		// the burn alerts. Measured: parked Workflows ended TimedOut and the SLO
		// alerts fired.
		//
		// That would teach the OPPOSITE of the truth. An execution timeout is
		// what CONVERTS an invisible stuck Workflow into a visible failed one,
		// and Temporal's default is no execution timeout at all. The genuinely
		// undetectable case — the one the Visibility monitor exists for — is a
		// Workflow that is Running, healthy, and simply never ends.
		//
		// So: an execution timeout is a good idea, and setting one is a real
		// mitigation. This scenario removes it to demonstrate what you are
		// exposed to when you do not.
		if req.StuckMode != "" {
			opts.WorkflowExecutionTimeout = 0
		}

		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()

		run, err := c.ExecuteWorkflow(ctx, opts, OrderWorkflow, req.OrderInput)
		if err != nil {
			// 503 rather than 500: a failure here usually means the Temporal
			// Frontend rejected the request (throttling, unavailable), which
			// is a dependency problem, not an application bug.
			writeJSON(w, http.StatusServiceUnavailable, startResponse{Error: err.Error()})
			return
		}

		resp := startResponse{
			WorkflowID: run.GetID(),
			RunID:      run.GetRunID(),
			TaskQueue:  queue,
		}

		if req.Wait {
			var result string
			if err := run.Get(ctx, &result); err != nil {
				resp.Error = err.Error()
				writeJSON(w, http.StatusOK, resp)
				return
			}
			resp.Result = result
		}

		writeJSON(w, http.StatusAccepted, resp)
	})

	// -------------------------------------------------------------------------
	// Agent runs
	//
	// A separate task queue from /orders, which is the point rather than an
	// implementation detail. Agent Activities block on a third-party API for
	// seconds at a time while order Activities finish in milliseconds; sharing a
	// queue means a burst of agent traffic holds every slot and starves order
	// processing, and the resulting schedule-to-start spike would appear on the
	// ORDER queue with nothing in the order path to explain it.
	//
	// Separate queues also make the two independently scalable, which is what
	// the scale benchmark in scripts/scale-benchmark.sh measures.
	// -------------------------------------------------------------------------
	agentQueue := env("AGENT_TASK_QUEUE", defaultAgentTaskQueue)

	mux.HandleFunc("/agent", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}

		var req agentRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
			return
		}
		if req.Prompt == "" {
			req.Prompt = "Where is order order-42 and what will it cost to ship to Bristol?"
		}

		queue := req.TaskQueue
		if queue == "" {
			queue = agentQueue
		}
		id := req.RunID
		if id == "" {
			id = fmt.Sprintf("%d", time.Now().UnixNano())
		}

		opts := client.StartWorkflowOptions{
			ID:        agentWorkflowIDPrefix + id,
			TaskQueue: queue,
			// Generous but finite. An agent that loops is the expensive failure
			// mode here, and unlike a stuck order Workflow it is spending money
			// the whole time it is invisible — so unlike /orders this endpoint
			// keeps a cap even for the chaos path.
			WorkflowExecutionTimeout: 30 * time.Minute,
		}

		ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
		defer cancel()

		run, err := c.ExecuteWorkflow(ctx, opts, AgentWorkflow, req.AgentInput)
		if err != nil {
			writeJSON(w, http.StatusServiceUnavailable, agentResponse{Error: err.Error()})
			return
		}

		resp := agentResponse{
			WorkflowID: run.GetID(),
			RunID:      run.GetRunID(),
			TaskQueue:  queue,
		}

		if req.Wait {
			var result AgentResult
			if err := run.Get(ctx, &result); err != nil {
				resp.Error = err.Error()
				writeJSON(w, http.StatusOK, resp)
				return
			}
			resp.Result = &result
		}

		writeJSON(w, http.StatusAccepted, resp)
	})

	addr := ":" + env("HTTP_PORT", "8081")
	log.Printf("api listening on %s (default task queue %q)", addr, defaultQueue)

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Fatalln(srv.ListenAndServe())
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}
