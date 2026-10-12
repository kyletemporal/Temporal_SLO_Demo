package main

import (
	"context"
	"errors"
	"math"
	"testing"

	"github.com/stretchr/testify/mock"
	"go.temporal.io/sdk/testsuite"
)

// These tests cover the three places where a bug is SILENT — where the code
// keeps running and the dashboard keeps rendering, just with wrong numbers. A
// panic would be caught by anyone; a cost panel that is 10x low would not be.

func TestEstimateCostUSD(t *testing.T) {
	// mock-1 is priced at $1/M input, $10/M output, $0.10/M cached read.
	// Round numbers so every expected value below can be checked by hand.
	tests := []struct {
		name  string
		model string
		usage TokenUsage
		want  float64
	}{
		{
			name:  "input and output at full rate",
			model: "mock-1",
			usage: TokenUsage{InputTokens: 1_000_000, OutputTokens: 1_000_000},
			want:  11.0,
		},
		{
			// THE REGRESSION THIS FILE EXISTS FOR.
			//
			// Cached tokens are a SUBSET of InputTokens and are billed at the
			// cheaper rate, so they must be subtracted from the full-rate
			// portion, not added alongside it. Adding gives 1.10; the correct
			// answer is 0.10 — a 10x overstatement, and always on exactly the
			// workloads that optimised hardest.
			name:  "fully cached input bills at the cache-read rate only",
			model: "mock-1",
			usage: TokenUsage{InputTokens: 1_000_000, CachedInputTokens: 1_000_000},
			want:  0.10,
		},
		{
			name:  "half cached",
			model: "mock-1",
			usage: TokenUsage{InputTokens: 1_000_000, CachedInputTokens: 500_000},
			want:  0.55, // 0.5M * $1 + 0.5M * $0.10
		},
		{
			// Reasoning tokens are already inside OutputTokens. Pricing them
			// again would double-bill every thinking model on the dashboard.
			name:  "reasoning tokens are not billed a second time",
			model: "mock-1",
			usage: TokenUsage{OutputTokens: 1_000_000, ReasoningTokens: 400_000},
			want:  10.0,
		},
		{
			// An unpriced model contributes nothing rather than a guess. The
			// resulting hole in the cost panel is the intended signal — see the
			// agentPricing comment.
			name:  "unknown model contributes zero rather than a guess",
			model: "some-model-nobody-priced",
			usage: TokenUsage{InputTokens: 1_000_000, OutputTokens: 1_000_000},
			want:  0,
		},
		{
			// Defensive: providers have shipped bugs where cached exceeds total.
			// Clamping keeps the counter monotonic; a negative Add would make
			// rate() produce a spike at the next scrape.
			name:  "cached exceeding input never produces a negative cost",
			model: "mock-1",
			usage: TokenUsage{InputTokens: 100, CachedInputTokens: 500},
			want:  0.00005, // 0 full-rate + 500 * $0.10/M
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := estimateCostUSD("mock", tc.model, tc.usage)
			if math.Abs(got-tc.want) > 1e-9 {
				t.Fatalf("estimateCostUSD = %v, want %v", got, tc.want)
			}
			if got < 0 {
				t.Fatalf("cost must never be negative, got %v", got)
			}
		})
	}
}

func TestClassifyLLMError(t *testing.T) {
	// The taxonomy drives BOTH the error.type metric label and the Workflow's
	// NonRetryableErrorTypes list. A change that breaks one breaks the other, so
	// the retryability expectation is asserted alongside the bucket.
	tests := []struct {
		err          error
		want         string
		nonRetryable bool
	}{
		{errors.New("HTTP 429: rate_limit_error"), "rate_limit", false},
		{errors.New("529 overloaded_error"), "overloaded", false},
		{errors.New("401 authentication_error: invalid x-api-key"), "auth", true},
		{errors.New("400 invalid_request_error"), "bad_request", true},
		{errors.New("refusal: model declined"), "refusal", true},
		{errors.New("context deadline exceeded"), "timeout", false},
		{errors.New("503 service unavailable"), "server_error", false},
		{errors.New("something nobody anticipated"), "unknown", false},
		{nil, "", false},
	}

	for _, tc := range tests {
		got := classifyLLMError(tc.err)
		if got != tc.want {
			t.Errorf("classifyLLMError(%v) = %q, want %q", tc.err, got, tc.want)
		}
		if tc.err == nil {
			continue
		}
		// Mirrors the switch in InvokeModel. If someone adds a bucket to one
		// and not the other, this fails rather than silently paying for three
		// attempts at an unretryable error.
		isNonRetryable := got == "auth" || got == "bad_request" || got == "refusal"
		if isNonRetryable != tc.nonRetryable {
			t.Errorf("classifyLLMError(%v) = %q: retryability mismatch", tc.err, got)
		}
	}
}

// TestAgentWorkflowLoop drives the real Workflow against mocked Activities.
//
// What this protects: the loop must stop. Every other property of an agent is
// recoverable, but a loop that does not terminate spends money for as long as
// nobody notices, and no Temporal metric moves while it does.
func TestAgentWorkflowLoop(t *testing.T) {
	t.Run("stops as soon as the model gives a final answer", func(t *testing.T) {
		var s testsuite.WorkflowTestSuite
		envt := s.NewTestWorkflowEnvironment()
		envt.RegisterWorkflow(AgentWorkflow)

		envt.OnActivity(InvokeModel, mock.Anything, mock.Anything).Return(
			LLMReply{Text: "done", StopReason: "end_turn", Usage: TokenUsage{InputTokens: 10, OutputTokens: 5}}, nil).Once()
		envt.OnActivity(ReportAgentRun, mock.Anything, mock.Anything).Return(nil)

		envt.ExecuteWorkflow(AgentWorkflow, AgentInput{Prompt: "hi", Provider: "mock"})

		if !envt.IsWorkflowCompleted() {
			t.Fatal("workflow did not complete")
		}
		if err := envt.GetWorkflowError(); err != nil {
			t.Fatalf("workflow error: %v", err)
		}
		var got AgentResult
		if err := envt.GetWorkflowResult(&got); err != nil {
			t.Fatal(err)
		}
		if got.Iterations != 1 {
			t.Errorf("iterations = %d, want 1", got.Iterations)
		}
		if got.StopReason != "end_turn" {
			t.Errorf("stopReason = %q, want end_turn", got.StopReason)
		}
	})

	t.Run("a model that only ever calls tools is stopped by the cap", func(t *testing.T) {
		// The runaway case. Without MaxIterations this runs until the execution
		// timeout, and the only visible symptom is the bill.
		var s testsuite.WorkflowTestSuite
		envt := s.NewTestWorkflowEnvironment()
		envt.RegisterWorkflow(AgentWorkflow)

		envt.OnActivity(InvokeModel, mock.Anything, mock.Anything).Return(
			LLMReply{
				StopReason: "tool_use",
				Usage:      TokenUsage{InputTokens: 100, OutputTokens: 20},
				ToolCalls:  []ToolCall{{ID: "c1", Name: "lookup_order", Arguments: `{"order_id":"x"}`}},
			}, nil)
		envt.OnActivity(RunAgentTool, mock.Anything, mock.Anything).Return(
			toolResponse{Content: "{}"}, nil)
		envt.OnActivity(ReportAgentRun, mock.Anything, mock.Anything).Return(nil)

		envt.ExecuteWorkflow(AgentWorkflow, AgentInput{Prompt: "hi", Provider: "mock", MaxIterations: 3})

		if !envt.IsWorkflowCompleted() {
			t.Fatal("workflow did not complete — the iteration cap did not hold")
		}
		var got AgentResult
		if err := envt.GetWorkflowResult(&got); err != nil {
			t.Fatal(err)
		}
		if got.Iterations != 3 {
			t.Errorf("iterations = %d, want 3", got.Iterations)
		}
		if got.StopReason != "max_iterations" {
			t.Errorf("stopReason = %q, want max_iterations — the outcome a runaway agent must be distinguishable by", got.StopReason)
		}
		// Usage must accumulate across iterations, not report only the last
		// call. Reporting the last call alone would make a 40-iteration runaway
		// look identical in cost to a 1-iteration answer.
		if got.Usage.InputTokens != 300 {
			t.Errorf("inputTokens = %d, want 300 (accumulated across 3 iterations)", got.Usage.InputTokens)
		}
	})
}

// Compile-time guard: the mock provider must never need a network or a key.
// If someone later gives it a real HTTP call, this stops passing in CI before
// it strands anyone running the demo offline.
func TestMockProviderNeedsNoCredentials(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "")
	t.Setenv("OPENAI_API_KEY", "")

	reply, err := invokeMock(context.Background(), LLMRequest{
		Model:    "mock-1",
		System:   "sys",
		Messages: []ChatMessage{{Role: RoleUser, Text: "where is my order?"}},
	})
	if err != nil {
		t.Fatalf("mock provider failed with no credentials: %v", err)
	}
	if reply.Usage.InputTokens <= 0 || reply.Usage.OutputTokens <= 0 {
		t.Fatalf("mock must report non-zero usage or the cost panel stays empty: %+v", reply.Usage)
	}
	if len(reply.ToolCalls) == 0 {
		t.Fatal("mock must exercise the tool path on the first turn")
	}
}
