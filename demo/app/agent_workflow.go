package main

import (
	"time"

	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
)

// AgentWorkflow is a tool-calling agent loop as a durable execution.
//
// # WHY THIS SHAPE
//
// The loop below is the same loop every agent framework writes: call the model,
// if it asked for tools run them, feed the results back, repeat. The difference
// is where the loop counter lives. In a web handler it lives in process memory,
// so a deploy, an OOM or a spot-instance reclaim halfway through a twelve-step
// agent loses every token already paid for and starts again. Here it lives in
// Workflow history, so the same crash resumes at iteration seven with the
// conversation intact.
//
// That is the argument for running agents on Temporal, and it is also why the
// observability differs from a normal service. The interesting failure is not a
// 500. It is a loop that never terminates, a model that calls the same tool
// forty times, or a nightly job whose token spend triples after a prompt change
// nobody flagged. None of those are errors, so none of them move an error rate.
//
// # WHAT LIVES WHERE
//
// The Workflow makes decisions and holds state; it performs no I/O and emits no
// metrics. Every network call and every metric emission is in an Activity. That
// split is not stylistic — a Workflow re-executes from the top on every replay,
// so a metric emitted here would be re-emitted once per replay and multiply the
// fleet's reported token spend by the replay count. The bug would be invisible
// in a lab (few replays) and severe in production (many).
func AgentWorkflow(ctx workflow.Context, in AgentInput) (AgentResult, error) {
	logger := workflow.GetLogger(ctx)

	maxIter := in.MaxIterations
	if maxIter <= 0 {
		maxIter = defaultMaxIterations
	}
	provider := in.Provider
	if provider == "" {
		provider = "mock"
	}
	model := in.Model
	if model == "" {
		// SideEffect records the value in history on first execution and
		// returns the RECORDED value on replay. defaultModelFor reads the
		// Worker's environment, which can differ across a rollout; calling it
		// directly recomputed the model on every replay.
		//
		// Behind GetVersion because adding a SideEffect changes the command
		// sequence: an agent run started on the old code and replayed on the
		// new would otherwise fail with exactly the non-determinism error
		// `make chaos-nde` demonstrates. Old runs keep the old path.
		if workflow.GetVersion(ctx, "model-recorded-in-side-effect", workflow.DefaultVersion, 1) == workflow.DefaultVersion {
			model = defaultModelFor(provider)
		} else {
			var resolved string
			if err := workflow.SideEffect(ctx, func(workflow.Context) any {
				return defaultModelFor(provider)
			}).Get(&resolved); err != nil {
				return AgentResult{}, err
			}
			model = resolved
		}
	}

	// Two Activity option sets, because model calls and tool calls fail in
	// genuinely different ways and a single policy has to be wrong for one.
	//
	// A model call is a slow, expensive, rate-limited third-party request.
	// StartToClose is generous (a high-effort call on a hard prompt can run for
	// minutes) and retries are few — a retried model call is a second full
	// charge, so retrying a prompt that the provider will refuse again just buys
	// the same refusal twice.
	llmCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 5 * time.Minute,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    2 * time.Second,
			BackoffCoefficient: 2.0,
			MaximumInterval:    30 * time.Second,
			MaximumAttempts:    3,
			// Retrying these is pure waste. A bad key is still bad in four
			// seconds, a malformed request is still malformed, and a content
			// refusal is a decision rather than a fault. Without this list the
			// default policy pays for three of each.
			NonRetryableErrorTypes: []string{"auth", "bad_request", "refusal"},
		},
	})

	// Tool calls are local, fast and cheap, so the opposite settings apply.
	toolCtx := workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: 30 * time.Second,
		RetryPolicy: &temporal.RetryPolicy{
			InitialInterval:    time.Second,
			BackoffCoefficient: 2.0,
			MaximumAttempts:    3,
		},
	})

	prompt := in.Prompt
	if in.LoopForever {
		// The chaos lever rides in the prompt so the provider contract stays a
		// plain LLMRequest. Against a real provider this is just an odd token in
		// the prompt and the model answers normally; only the mock acts on it.
		prompt += " [loop-forever]"
	}
	messages := []ChatMessage{{Role: RoleUser, Text: prompt}}
	tools := agentToolSpecs()

	var total TokenUsage
	result := AgentResult{StopReason: "max_iterations"}

	for i := 1; i <= maxIter; i++ {
		result.Iterations = i

		var reply LLMReply
		err := workflow.ExecuteActivity(llmCtx, InvokeModel, LLMRequest{
			Provider:  provider,
			Model:     model,
			System:    agentSystemPrompt,
			Messages:  messages,
			Tools:     tools,
			MaxTokens: 4096,
			Effort:    in.Effort,
		}).Get(ctx, &reply)
		if err != nil {
			// Report the run before returning. A run that dies on a provider
			// outage still consumed tokens and still belongs in the histogram —
			// dropping it makes the failure look cheaper than it was.
			_ = workflow.ExecuteActivity(toolCtx, ReportAgentRun,
				agentRunReport{Outcome: "error", StopReason: classifyLLMError(err), Iterations: i}).Get(ctx, nil)
			return result, err
		}

		total.InputTokens += reply.Usage.InputTokens
		total.OutputTokens += reply.Usage.OutputTokens
		total.ReasoningTokens += reply.Usage.ReasoningTokens
		total.CachedInputTokens += reply.Usage.CachedInputTokens
		result.Usage = total

		// No tool calls means the model is done talking.
		if len(reply.ToolCalls) == 0 {
			result.Answer = reply.Text
			result.StopReason = reply.StopReason
			_ = workflow.ExecuteActivity(toolCtx, ReportAgentRun,
				agentRunReport{Outcome: "completed", StopReason: reply.StopReason, Iterations: i}).Get(ctx, nil)
			logger.Info("agent finished", "iterations", i, "inputTokens", total.InputTokens, "outputTokens", total.OutputTokens)
			return result, nil
		}

		messages = append(messages, ChatMessage{
			Role:      RoleAssistant,
			Text:      reply.Text,
			ToolCalls: reply.ToolCalls,
		})

		// Fan the tool calls out concurrently.
		//
		// Both providers can return several tool_use blocks in one response and
		// running them in series makes the agent as slow as the sum of its
		// tools. Futures are collected in the order they were STARTED, not the
		// order they complete: a Workflow must produce identical results on
		// replay, and completion order is not stable across a replay.
		futures := make([]workflow.Future, len(reply.ToolCalls))
		for j, call := range reply.ToolCalls {
			futures[j] = workflow.ExecuteActivity(toolCtx, RunAgentTool, toolRequest{
				Call:         call,
				FailToolRate: in.FailToolRate,
			})
		}

		for j, f := range futures {
			var out toolResponse
			if err := f.Get(ctx, &out); err != nil {
				// A tool that fails after its retries is reported back to the
				// model as an error RESULT, not raised as a Workflow failure.
				// That is what lets the agent recover — and the recovery is
				// itself billable, which is why tool failure rate belongs on the
				// cost dashboard and not only on the error one.
				out = toolResponse{Content: "tool error: " + err.Error(), IsError: true}
			}
			messages = append(messages, ChatMessage{
				Role:       RoleTool,
				Text:       out.Content,
				ToolCallID: reply.ToolCalls[j].ID,
				IsError:    out.IsError,
			})
		}

		// CHAOS: the runaway agent. See AgentInput.LoopForever.
		//
		// Nothing here fails, so no Temporal metric moves: no Activity failure,
		// no Workflow failure, no retry, no schedule-to-start growth. The only
		// signals that change are token spend and loop depth — which is the
		// entire case for instrumenting an agent beyond its Workflow.
		if in.LoopForever && i == maxIter {
			logger.Warn("agent hit iteration cap without a final answer",
				"iterations", i, "inputTokens", total.InputTokens)
		}
	}

	// Falling out of the loop means the cap stopped it, not the model. That is a
	// distinct outcome from "completed" and the dashboard splits on it: a fleet
	// where this rises is either under-capped or stuck in a tool loop, and both
	// cost money silently.
	_ = workflow.ExecuteActivity(toolCtx, ReportAgentRun,
		agentRunReport{Outcome: "max_iterations", StopReason: "max_iterations", Iterations: maxIter}).Get(ctx, nil)
	return result, nil
}

const agentSystemPrompt = `You are a logistics assistant for an order system.
Use the provided tools to answer questions about orders, inventory and shipping.
Call a tool when you need a fact you do not have; answer directly when you do.
Keep answers to two sentences.`

// defaultModelFor reads the Worker's environment, which is stable for the life
// of a Worker but NOT across a fleet — a rollout that changes ANTHROPIC_MODEL
// changes what it returns. It is therefore only ever called inside
// workflow.SideEffect (see AgentWorkflow), which records the first result in
// history and replays THAT.
//
// An earlier comment here claimed the model was "recorded into the Activity
// input and reused from history" and that the API handler set it. Neither was
// true: the Go SDK does not compare Activity inputs on replay, the value was
// recomputed from env every replay, and the handler never set Model.
func defaultModelFor(provider string) string {
	switch provider {
	case "anthropic":
		return env("ANTHROPIC_MODEL", "claude-opus-5")
	case "openai":
		// No default that is right for everyone, and naming a model this repo
		// has not priced would put a confident zero on the cost panel. Set
		// OPENAI_MODEL explicitly and add its list price to agentPricing.
		return env("OPENAI_MODEL", "gpt-4.1-mini")
	default:
		return "mock-1"
	}
}
