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

type startResponse struct {
	WorkflowID string `json:"workflowId"`
	RunID      string `json:"runId"`
	TaskQueue  string `json:"taskQueue"`
	Result     string `json:"result,omitempty"`
	Error      string `json:"error,omitempty"`
}

func runAPI() {
	scope := newPrometheusScope("0.0.0.0:" + env("METRICS_PORT", "8077"))

	c, err := dialTemporal(client.Options{
		HostPort:       env("TEMPORAL_ADDRESS", "localhost:7233"),
		Namespace:      env("TEMPORAL_NAMESPACE", "default"),
		MetricsHandler: sdktally.NewMetricsHandler(scope),
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
