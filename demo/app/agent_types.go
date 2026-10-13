package main

import (
	"errors"
	"net"
	"strings"
)

// Provider-neutral conversation types.
//
// These exist because of a Temporal constraint, and the constraint turns out to
// be the good part of the design.
//
// The agent loop is a WORKFLOW, so the conversation lives in Workflow history —
// that is what makes the agent durable, and it is the whole reason to run an
// agent on Temporal rather than in a for-loop in a web handler. But Workflow
// state has to round-trip through the data converter, and provider SDK message
// types are not built for that: they carry unexported fields, union wrappers and
// param.Opt sentinels that do not survive JSON.
//
// So the Workflow owns these plain structs and each Activity translates at the
// edge. The side effect is that swapping Anthropic for OpenAI changes one
// Activity and no Workflow code, and a running agent can switch providers
// between iterations without breaking replay — the history contains no
// provider-specific bytes at all.

type ChatRole string

const (
	RoleSystem    ChatRole = "system"
	RoleUser      ChatRole = "user"
	RoleAssistant ChatRole = "assistant"
	RoleTool      ChatRole = "tool"
)

// ChatMessage is one conversational turn.
//
// A single struct covers all four roles rather than an interface, because a
// union type would have to survive JSON round-tripping through Workflow history
// and Go has no clean way to do that without a discriminator field anyway.
type ChatMessage struct {
	Role ChatRole `json:"role"`
	Text string   `json:"text,omitempty"`

	// ToolCalls is set on assistant turns where the model asked for tools.
	ToolCalls []ToolCall `json:"toolCalls,omitempty"`

	// Set on tool-result turns. ToolCallID must match the ID the model issued;
	// both providers reject a result whose ID it never sent.
	ToolCallID string `json:"toolCallId,omitempty"`
	IsError    bool   `json:"isError,omitempty"`
}

type ToolCall struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	// Arguments is the raw JSON object the model produced, kept as a string
	// rather than map[string]any so it round-trips through Workflow history
	// byte-for-byte. Re-marshalling a decoded map reorders keys, and a reordered
	// argument blob is a different value on replay.
	Arguments string `json:"arguments"`
}

type ToolSpec struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	Schema      map[string]any `json:"schema"`
}

type TokenUsage struct {
	InputTokens  int64 `json:"inputTokens"`
	OutputTokens int64 `json:"outputTokens"`

	// ReasoningTokens is a SUBSET of OutputTokens, and CachedInputTokens a
	// subset of InputTokens. Both are reported separately for attribution and
	// must never be added to the totals — see recordLLMCall.
	ReasoningTokens   int64 `json:"reasoningTokens,omitempty"`
	CachedInputTokens int64 `json:"cachedInputTokens,omitempty"`
}

// LLMRequest is what crosses into the Activity.
type LLMRequest struct {
	Provider  string        `json:"provider"`
	Model     string        `json:"model"`
	System    string        `json:"system,omitempty"`
	Messages  []ChatMessage `json:"messages"`
	Tools     []ToolSpec    `json:"tools,omitempty"`
	MaxTokens int64         `json:"maxTokens,omitempty"`

	// Effort maps to output_config.effort on Anthropic and reasoning_effort on
	// OpenAI. Same word, same intent, different request field — which is exactly
	// the kind of divergence this layer exists to absorb.
	Effort string `json:"effort,omitempty"`
}

type LLMReply struct {
	Text       string     `json:"text"`
	Model      string     `json:"model"`
	ToolCalls  []ToolCall `json:"toolCalls,omitempty"`
	StopReason string     `json:"stopReason"`
	Usage      TokenUsage `json:"usage"`
}

// AgentInput drives AgentWorkflow. The chaos levers follow the same convention
// as OrderInput: every field maps to something visible on a specific panel.
type AgentInput struct {
	Prompt string `json:"prompt"`

	// Provider selects the Activity implementation: "anthropic", "openai", or
	// "mock". Mock is the default precisely so the demo runs with no API keys —
	// an observability lab that requires a funded account to show its dashboard
	// is not a lab most people will ever run.
	Provider string `json:"provider,omitempty"`
	Model    string `json:"model,omitempty"`
	Effort   string `json:"effort,omitempty"`

	// MaxIterations caps the agent loop. This is a COST control, not a
	// correctness one: an agent that calls a tool whose result never satisfies
	// it will loop until something stops it, and every iteration is billed.
	// Drives the loop-iterations histogram and the runaway alert.
	MaxIterations int `json:"maxIterations,omitempty"`

	// FailToolRate injects tool failures (0.0–1.0). Tool errors are handed back
	// to the model as content rather than failing the Activity, so this shows
	// the model recovering — and shows the retry cost of that recovery in
	// tokens, which is the part teams do not budget for.
	FailToolRate float64 `json:"failToolRate,omitempty"`

	// LoopForever makes the mock provider never emit a final answer. The
	// runaway-agent scenario: nothing fails, nothing retries, no Temporal metric
	// moves, and spend climbs linearly. The agent equivalent of StuckMode
	// "parked" — and the reason gen_ai.agent.loop.iterations exists at all.
	LoopForever bool `json:"loopForever,omitempty"`
}

type AgentResult struct {
	Answer     string     `json:"answer"`
	Iterations int        `json:"iterations"`
	Usage      TokenUsage `json:"usage"`
	StopReason string     `json:"stopReason"`
}

const (
	defaultAgentTaskQueue = "agents"
	defaultMaxIterations  = 8
	agentWorkflowIDPrefix = "agent-"
)

// classifyLLMError buckets an error into a low-cardinality error.type value.
//
// Low cardinality is the whole point. The obvious implementation puts err.Error()
// on the metric, and the first rate-limit response with a request ID in the body
// creates a new time series per request — which is how a metrics bill outgrows
// the LLM bill it was meant to watch.
//
// The buckets are chosen to match what an operator actually does next, which is
// the only test that matters for an error taxonomy:
//
//	rate_limit  back off, or raise the account limit
//	overloaded  retry; provider-side, nothing to fix locally
//	auth        a human must rotate a key; retrying is pointless
//	timeout     raise the Activity timeout, or lower effort
//	refusal     a content decision, not a fault — never retry the same prompt
//	bad_request malformed call; a code bug, and also never retryable
func classifyLLMError(err error) string {
	if err == nil {
		return ""
	}

	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "timeout"
	}

	msg := strings.ToLower(err.Error())
	switch {
	case strings.Contains(msg, "429"), strings.Contains(msg, "rate_limit"), strings.Contains(msg, "rate limit"):
		return "rate_limit"
	case strings.Contains(msg, "529"), strings.Contains(msg, "overloaded"):
		return "overloaded"
	case strings.Contains(msg, "401"), strings.Contains(msg, "403"),
		strings.Contains(msg, "authentication"), strings.Contains(msg, "permission"):
		return "auth"
	case strings.Contains(msg, "context deadline"), strings.Contains(msg, "timeout"):
		return "timeout"
	case strings.Contains(msg, "refusal"):
		return "refusal"
	case strings.Contains(msg, "400"), strings.Contains(msg, "invalid_request"):
		return "bad_request"
	case strings.Contains(msg, "500"), strings.Contains(msg, "502"), strings.Contains(msg, "503"):
		return "server_error"
	default:
		return "unknown"
	}
}

// modelPrice is USD per million tokens.
type modelPrice struct {
	input      float64
	output     float64
	cachedRead float64
}

// agentPricing is a LIST-PRICE lookup, and every word of that matters.
//
// The number on the cost panel is an ESTIMATE at published rates. It will not
// match an invoice, for reasons none of which this code can see: negotiated
// discounts, committed-use terms, batch pricing at half rate, and free tiers all
// move the real figure. Treat the panel as a trend and an attribution tool — is
// spend rising, and which model is doing it — not as a finance source of truth.
//
// The table is deliberately small and deliberately explicit. Guessing a price
// for an unrecognised model would put a confident wrong number on a dashboard,
// which is worse than a gap; an unknown model contributes 0 and shows up as a
// visible hole in the cost panel that a human can then fix here.
var agentPricing = map[string]modelPrice{
	// Anthropic (platform.claude.com/docs/en/pricing)
	"claude-opus-5":    {input: 5.00, output: 25.00, cachedRead: 0.50},
	"claude-opus-4-8":  {input: 5.00, output: 25.00, cachedRead: 0.50},
	"claude-sonnet-5":  {input: 3.00, output: 15.00, cachedRead: 0.30},
	"claude-haiku-4-5": {input: 1.00, output: 5.00, cachedRead: 0.10},
	"claude-fable-5":   {input: 10.00, output: 50.00, cachedRead: 1.00},

	// The mock provider is priced so the cost panel is populated and its maths
	// is checkable offline. Round numbers make a hand-verified assertion
	// possible in validate.sh.
	"mock-1": {input: 1.00, output: 10.00, cachedRead: 0.10},
}

// estimateCostUSD prices one call.
//
// Cached input tokens are billed at the cache-read rate and are a subset of
// InputTokens, so they are subtracted out before the full-rate multiply rather
// than added on top. Getting this backwards overstates the cost of exactly the
// workloads that optimised hardest, which would make the panel argue against
// caching.
func estimateCostUSD(provider, model string, u TokenUsage) float64 {
	p, ok := agentPricing[model]
	if !ok {
		return 0
	}

	fullRateInput := u.InputTokens - u.CachedInputTokens
	if fullRateInput < 0 {
		fullRateInput = 0
	}

	const perMillion = 1_000_000.0
	return (float64(fullRateInput)*p.input +
		float64(u.CachedInputTokens)*p.cachedRead +
		float64(u.OutputTokens)*p.output) / perMillion
}
