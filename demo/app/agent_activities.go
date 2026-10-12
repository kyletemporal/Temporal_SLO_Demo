package main

import (
	"context"
	"encoding/json"
	"fmt"
	"hash/fnv"
	"math/rand"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
)

// ---------------------------------------------------------------------------
// Tools
//
// Three toy tools with different cost shapes, so the dashboard has something to
// distinguish. lookup_order is fast and always works; check_inventory is slow
// enough to show up in tool latency; quote_shipping is the one that fails.
// ---------------------------------------------------------------------------

func agentToolSpecs() []ToolSpec {
	return []ToolSpec{
		{
			Name:        "lookup_order",
			Description: "Look up the status and contents of an order by its ID.",
			Schema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"order_id": map[string]any{"type": "string", "description": "The order identifier."},
				},
				"required": []string{"order_id"},
			},
		},
		{
			Name:        "check_inventory",
			Description: "Check warehouse stock for a SKU.",
			Schema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"sku": map[string]any{"type": "string", "description": "The stock keeping unit."},
				},
				"required": []string{"sku"},
			},
		},
		{
			Name:        "quote_shipping",
			Description: "Quote a shipping cost and ETA to a destination.",
			Schema: map[string]any{
				"type": "object",
				"properties": map[string]any{
					"destination": map[string]any{"type": "string", "description": "Destination city or postcode."},
					"weight_kg":   map[string]any{"type": "number", "description": "Parcel weight in kilograms."},
				},
				"required": []string{"destination"},
			},
		},
	}
}

type toolRequest struct {
	Call         ToolCall `json:"call"`
	FailToolRate float64  `json:"failToolRate,omitempty"`
}

type toolResponse struct {
	Content string `json:"content"`
	IsError bool   `json:"isError,omitempty"`
}

// RunAgentTool executes one tool call.
//
// A tool that fails returns an ERROR RESULT rather than an Activity error, in
// most cases: the model is supposed to see the failure and adapt, and converting
// every tool fault into a Workflow failure removes the agent's whole reason to
// exist. The exception is the injected-chaos path, which raises a real Activity
// error so the retry policy is exercised too.
func RunAgentTool(ctx context.Context, req toolRequest) (toolResponse, error) {
	log := activity.GetLogger(ctx)

	// rand is fine here and would NOT be fine in the Workflow. An Activity may
	// be retried but is never replayed for determinism, so non-determinism is
	// contained; the same call inside a Workflow would diverge on replay and
	// fail the Workflow Task.
	if req.FailToolRate > 0 && rand.Float64() < req.FailToolRate {
		recordToolCall(ctx, req.Call.Name, "error")
		log.Warn("injected tool failure", "tool", req.Call.Name)
		return toolResponse{}, temporal.NewApplicationError(
			fmt.Sprintf("tool %s is unavailable", req.Call.Name), "tool_unavailable")
	}

	var args map[string]any
	if req.Call.Arguments != "" {
		if err := json.Unmarshal([]byte(req.Call.Arguments), &args); err != nil {
			// A model that emits malformed arguments is a real and common
			// failure. It is handed back as content, not raised: retrying the
			// tool cannot fix the model's JSON, but telling the model can.
			recordToolCall(ctx, req.Call.Name, "bad_arguments")
			return toolResponse{
				Content: fmt.Sprintf("could not parse arguments: %v", err),
				IsError: true,
			}, nil
		}
	}

	var out string
	switch req.Call.Name {
	case "lookup_order":
		id, _ := args["order_id"].(string)
		if id == "" {
			recordToolCall(ctx, req.Call.Name, "bad_arguments")
			return toolResponse{Content: "order_id is required", IsError: true}, nil
		}
		out = fmt.Sprintf(`{"order_id":%q,"status":"in_transit","items":2,"sku":"SKU-114"}`, id)

	case "check_inventory":
		sku, _ := args["sku"].(string)
		// Deliberately slow: an agent's latency is usually dominated by tools,
		// not by the model, and a flat tool-latency panel hides that.
		select {
		case <-time.After(250 * time.Millisecond):
		case <-ctx.Done():
			recordToolCall(ctx, req.Call.Name, "cancelled")
			return toolResponse{}, ctx.Err()
		}
		out = fmt.Sprintf(`{"sku":%q,"on_hand":47,"reserved":6,"warehouse":"LHR-2"}`, sku)

	case "quote_shipping":
		dest, _ := args["destination"].(string)
		if dest == "" {
			recordToolCall(ctx, req.Call.Name, "bad_arguments")
			return toolResponse{Content: "destination is required", IsError: true}, nil
		}
		out = fmt.Sprintf(`{"destination":%q,"cost_gbp":6.40,"eta_days":3}`, dest)

	default:
		// A model asking for a tool that does not exist is worth its own outcome
		// value: it usually means the prompt and the tool list have drifted
		// apart, which no error rate will ever show you.
		recordToolCall(ctx, req.Call.Name, "unknown_tool")
		return toolResponse{
			Content: fmt.Sprintf("no such tool: %s", req.Call.Name),
			IsError: true,
		}, nil
	}

	recordToolCall(ctx, req.Call.Name, "ok")
	return toolResponse{Content: out}, nil
}

// ---------------------------------------------------------------------------
// Model invocation
// ---------------------------------------------------------------------------

type agentRunReport struct {
	Outcome    string `json:"outcome"`
	StopReason string `json:"stopReason"`
	Iterations int    `json:"iterations"`
}

// ReportAgentRun closes out a run's metrics.
//
// It is an Activity purely so the emission happens exactly once. Doing it in the
// Workflow would re-emit on every replay; doing it in the API process would miss
// runs that finish after the caller has gone.
func ReportAgentRun(ctx context.Context, r agentRunReport) error {
	recordAgentRun(ctx, "order-assistant", r.Outcome, r.StopReason, r.Iterations)
	return nil
}

// InvokeModel dispatches to a provider and records the call.
//
// The timing brackets the provider call ONLY. Including the Activity's own
// scheduling would fold Temporal queue latency into a metric labelled as model
// latency, and the resulting panel would blame the provider for a backlog in
// your own Worker fleet — the exact misattribution the rest of this repo exists
// to prevent.
func InvokeModel(ctx context.Context, req LLMRequest) (LLMReply, error) {
	start := time.Now()

	var (
		reply LLMReply
		err   error
	)
	switch req.Provider {
	case "anthropic":
		reply, err = invokeAnthropic(ctx, req)
	case "openai":
		reply, err = invokeOpenAI(ctx, req)
	case "mock", "":
		reply, err = invokeMock(ctx, req)
	default:
		// Assigned rather than returned, so it falls through to recordLLMCall
		// below like every other failure.
		//
		// This branch originally returned early, and the gap was caught by
		// tools/check_dashboard_queries.py: a deliberately-triggered bad
		// provider produced a failed Workflow and NO error metric at all. A
		// misconfigured provider name is exactly the kind of deploy-time
		// mistake the error panel exists to surface, and it was the one error
		// the panel could not see.
		err = fmt.Errorf("bad_request: unknown provider %q", req.Provider)
	}

	recordLLMCall(ctx, reply, req.Provider, req.Model, time.Since(start), err)

	if err != nil {
		// The classified bucket becomes the Application Error TYPE, which is
		// what the Workflow's NonRetryableErrorTypes list matches on. Same
		// taxonomy drives the metric and the retry decision, so a change to one
		// cannot silently diverge from the other.
		kind := classifyLLMError(err)
		nonRetryable := kind == "auth" || kind == "bad_request" || kind == "refusal"
		return LLMReply{}, temporal.NewApplicationErrorWithOptions(
			err.Error(), kind, temporal.ApplicationErrorOptions{NonRetryable: nonRetryable})
	}
	return reply, nil
}

// ---------------------------------------------------------------------------
// Mock provider
//
// This is the default, and that is a deliberate product decision rather than a
// testing convenience. An observability lab whose headline dashboard requires a
// funded API account is a lab most people never see working. The mock produces
// realistic token counts, realistic tool-calling behaviour and realistic latency
// with no key, no network and no spend — so `make agent-demo` populates every
// panel on a laptop in an aeroplane.
//
// It is deterministic on the prompt so a scale run is reproducible: the same
// prompt yields the same tool sequence and the same token counts, which is what
// makes a before/after comparison mean anything.
// ---------------------------------------------------------------------------

func invokeMock(ctx context.Context, req LLMRequest) (LLMReply, error) {
	if len(req.Messages) == 0 {
		return LLMReply{}, fmt.Errorf("bad_request: no messages in request")
	}

	// Latency drawn from the prompt hash rather than a clock, so a benchmark
	// re-run is comparable. Roughly 150ms–1.1s, the shape of a small real model.
	h := fnv.New32a()
	_, _ = h.Write([]byte(req.Messages[0].Text))
	seed := int64(h.Sum32())
	rng := rand.New(rand.NewSource(seed + int64(len(req.Messages))))

	delay := time.Duration(150+rng.Intn(950)) * time.Millisecond
	select {
	case <-time.After(delay):
	case <-ctx.Done():
		return LLMReply{}, ctx.Err()
	}

	// Token counts scale with conversation length, which is the property that
	// makes the cost panel behave like a real one: a long agent loop re-sends
	// its whole history every iteration, so input tokens grow QUADRATICALLY with
	// loop depth. That is the single most surprising line on an LLM invoice and
	// a flat-token mock would hide it completely.
	var convChars int
	for _, m := range req.Messages {
		convChars += len(m.Text)
	}
	inputTokens := int64(len(req.System)/4 + convChars/4 + 120)
	outputTokens := int64(40 + rng.Intn(160))

	// Simulated prompt-cache hits on the stable prefix (system + first user
	// turn) once the conversation is long enough for a cache to exist.
	var cached int64
	if len(req.Messages) > 2 {
		cached = int64(len(req.System)/4 + len(req.Messages[0].Text)/4)
	}

	// Reasoning tokens: a share of output, as a thinking model produces.
	//
	// Included because without them the "Reasoning token share" panel is dark
	// for anyone running the demo without an API key — which is the default
	// path and therefore most people. A permanently empty panel teaches the
	// reader that the metric does not work.
	//
	// The share is REALISTIC rather than flattering: reasoning routinely
	// dominates output on a hard prompt, and the panel's whole purpose is to
	// show spend that never appears in the response body.
	reasoning := int64(float64(outputTokens) * (0.3 + rng.Float64()*0.4))

	usage := TokenUsage{
		InputTokens:       inputTokens,
		OutputTokens:      outputTokens,
		ReasoningTokens:   reasoning,
		CachedInputTokens: cached,
	}

	// Tool-calling behaviour: ask for tools on the first two iterations, then
	// answer. Counting assistant turns is the deterministic way to know which
	// iteration this is — the Activity is stateless and cannot count for itself.
	var assistantTurns int
	for _, m := range req.Messages {
		if m.Role == RoleAssistant {
			assistantTurns++
		}
	}

	// LoopForever is signalled through the prompt rather than a request field so
	// the mock needs no knowledge of AgentInput. Crude, but it keeps the chaos
	// lever out of the provider contract that the real providers also implement.
	loopForever := strings.Contains(req.Messages[0].Text, "[loop-forever]")

	if loopForever || assistantTurns < 2 {
		tool := agentToolSpecs()[assistantTurns%len(agentToolSpecs())]
		args := map[string]string{
			"lookup_order":    `{"order_id":"order-42"}`,
			"check_inventory": `{"sku":"SKU-114"}`,
			"quote_shipping":  `{"destination":"Bristol","weight_kg":1.2}`,
		}[tool.Name]

		return LLMReply{
			Model:      req.Model,
			Text:       "Checking " + tool.Name + ".",
			StopReason: "tool_use",
			Usage:      usage,
			ToolCalls: []ToolCall{{
				ID:        fmt.Sprintf("call_%d_%d", seed%100000, assistantTurns),
				Name:      tool.Name,
				Arguments: args,
			}},
		}, nil
	}

	return LLMReply{
		Model:      req.Model,
		Text:       "Order order-42 is in transit with 2 items; shipping to Bristol is GBP 6.40 and arrives in 3 days.",
		StopReason: "end_turn",
		Usage:      usage,
	}, nil
}
