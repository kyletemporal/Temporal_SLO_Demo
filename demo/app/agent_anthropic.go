package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sync"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
)

// Anthropic provider — github.com/anthropics/anthropic-sdk-go v1.69.0.
//
// This file is one of three implementations of the same contract (see
// agent_openai.go and invokeMock). They exist side by side on purpose: the point
// of the dashboard is that it cannot tell them apart, and the only way to show
// that is to run more than one.
//
// Translation happens here rather than in the Workflow, so everything below is
// SDK-shaped and none of it reaches Workflow history.

var (
	anthropicOnce   sync.Once
	anthropicClient anthropic.Client
)

func anthropicAPI() *anthropic.Client {
	anthropicOnce.Do(func() {
		// A bare NewClient() already resolves ANTHROPIC_API_KEY from the
		// environment, and also an `ant auth login` profile if one exists. The
		// explicit option is here only so an unset key produces a clear failure
		// at call time rather than a confusing 401.
		var opts []option.RequestOption
		if key := os.Getenv("ANTHROPIC_API_KEY"); key != "" {
			opts = append(opts, option.WithAPIKey(key))
		}
		anthropicClient = anthropic.NewClient(opts...)
	})
	return &anthropicClient
}

func invokeAnthropic(ctx context.Context, req LLMRequest) (LLMReply, error) {
	if os.Getenv("ANTHROPIC_API_KEY") == "" {
		return LLMReply{}, fmt.Errorf("authentication: ANTHROPIC_API_KEY is not set")
	}

	maxTokens := req.MaxTokens
	if maxTokens <= 0 {
		maxTokens = 4096
	}

	params := anthropic.MessageNewParams{
		Model:     anthropic.Model(req.Model),
		MaxTokens: maxTokens,
		Messages:  toAnthropicMessages(req.Messages),

		// Adaptive thinking rather than a token budget. budget_tokens is
		// REMOVED on Claude Opus 5 and returns a 400 — the model decides depth
		// for itself and `effort` is the dial.
		//
		// Display "summarized" is set explicitly because the default is
		// "omitted", which streams thinking blocks whose text is empty. That is
		// fine for a batch job and reads as a long stall in anything a human
		// watches.
		Thinking: anthropic.ThinkingConfigParamUnion{
			OfAdaptive: &anthropic.ThinkingConfigAdaptiveParam{
				Display: anthropic.ThinkingConfigAdaptiveDisplaySummarized,
			},
		},
	}

	// temperature / top_p / top_k are deliberately absent. They are removed on
	// Claude Opus 5 and any value at all returns a 400; steer with the prompt.

	if req.System != "" {
		params.System = []anthropic.TextBlockParam{{Text: req.System}}
	}
	if req.Effort != "" {
		params.OutputConfig = anthropic.OutputConfigParam{
			Effort: anthropic.OutputConfigEffort(req.Effort),
		}
	}

	for _, t := range req.Tools {
		tool := anthropic.ToolParam{
			Name:        t.Name,
			Description: anthropic.String(t.Description),
			InputSchema: anthropic.ToolInputSchemaParam{
				Properties: t.Schema["properties"],
				Required:   toStringSlice(t.Schema["required"]),
			},
		}
		params.Tools = append(params.Tools, anthropic.ToolUnionParam{OfTool: &tool})
	}

	resp, err := anthropicAPI().Messages.New(ctx, params)
	if err != nil {
		return LLMReply{}, err
	}

	out := LLMReply{
		Model:      string(resp.Model),
		StopReason: string(resp.StopReason),
		Usage: TokenUsage{
			InputTokens:  resp.Usage.InputTokens,
			OutputTokens: resp.Usage.OutputTokens,

			// Thinking tokens are billed as output but never appear in the
			// response body, so a bill that grows while visible output does not
			// is unattributable without this field.
			ReasoningTokens: resp.Usage.OutputTokensDetails.ThinkingTokens,

			// Cache READS only. Cache creation is billed at a PREMIUM over
			// normal input (1.25x for the 5-minute TTL), so folding writes in
			// here would report cache activity as a saving at the exact moment
			// it is costing extra.
			CachedInputTokens: resp.Usage.CacheReadInputTokens,
		},
	}

	// A refusal arrives as a successful HTTP 200 with stop_reason "refusal",
	// not as an error. Code that reads content[0] unconditionally breaks here,
	// and code that only checks err treats a refusal as an empty answer.
	if resp.StopReason == anthropic.StopReasonRefusal {
		return out, fmt.Errorf("refusal: model declined (category %q)", resp.StopDetails.Category)
	}

	for _, block := range resp.Content {
		switch v := block.AsAny().(type) {
		case anthropic.TextBlock:
			out.Text += v.Text
		case anthropic.ToolUseBlock:
			// JSON.Input.Raw() gives the arguments exactly as the model emitted
			// them. Decoding to a map and re-marshalling would reorder keys, and
			// this string goes into Workflow history where a reordered value is
			// a different value on replay.
			out.ToolCalls = append(out.ToolCalls, ToolCall{
				ID:        v.ID,
				Name:      v.Name,
				Arguments: v.JSON.Input.Raw(),
			})
		}
	}
	return out, nil
}

// toAnthropicMessages converts the neutral history into SDK params.
//
// The shape differs from OpenAI in one way that matters: a tool RESULT is a
// content block inside a USER message, not a message with its own role. Several
// consecutive tool results therefore collapse into a single user turn, and
// splitting them into separate messages is the usual reason a hand-rolled
// Anthropic agent starts failing on its second tool call.
func toAnthropicMessages(msgs []ChatMessage) []anthropic.MessageParam {
	var out []anthropic.MessageParam
	var pendingResults []anthropic.ContentBlockParamUnion

	flush := func() {
		if len(pendingResults) > 0 {
			out = append(out, anthropic.NewUserMessage(pendingResults...))
			pendingResults = nil
		}
	}

	for _, m := range msgs {
		switch m.Role {
		case RoleTool:
			pendingResults = append(pendingResults,
				anthropic.NewToolResultBlock(m.ToolCallID, m.Text, m.IsError))

		case RoleUser:
			flush()
			out = append(out, anthropic.NewUserMessage(anthropic.NewTextBlock(m.Text)))

		case RoleAssistant:
			flush()
			var blocks []anthropic.ContentBlockParamUnion
			if m.Text != "" {
				blocks = append(blocks, anthropic.NewTextBlock(m.Text))
			}
			for _, tc := range m.ToolCalls {
				var input any
				if tc.Arguments != "" {
					_ = json.Unmarshal([]byte(tc.Arguments), &input)
				}
				blocks = append(blocks, anthropic.NewToolUseBlock(tc.ID, input, tc.Name))
			}
			if len(blocks) > 0 {
				out = append(out, anthropic.NewAssistantMessage(blocks...))
			}
		}
	}
	flush()
	return out
}

func toStringSlice(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			if s, ok := e.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}
