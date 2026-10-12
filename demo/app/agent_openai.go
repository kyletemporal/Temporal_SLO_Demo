package main

import (
	"context"
	"fmt"
	"os"
	"sync"

	"github.com/openai/openai-go/v3"
	"github.com/openai/openai-go/v3/option"
	"github.com/openai/openai-go/v3/shared"
)

// OpenAI provider — github.com/openai/openai-go/v3 v3.55.0.
//
// Second of the three implementations of the same contract. Reading this file
// next to agent_anthropic.go is the point of having both: the two APIs disagree
// about almost every detail below, and none of that disagreement escapes into
// the Workflow, the metrics, or the dashboard.
//
// The differences that actually cost people time:
//
//	tool results     a message with role "tool" here; a content block inside a
//	                 USER message on Anthropic
//	arguments        a JSON string on the tool call here; a decoded object there
//	token fields     prompt/completion here, input/output there
//	reasoning        completion_tokens_details.reasoning_tokens here,
//	                 output_tokens_details.thinking_tokens there
//	cached input     prompt_tokens_details.cached_tokens here,
//	                 cache_read_input_tokens there
//	system prompt    a message in the array here; a top-level field there
//	effort           reasoning_effort here; output_config.effort there
//	max tokens       optional here; REQUIRED there
//
// Eight divergences for one logical call. That is the argument for normalising
// at the edge instead of letting each provider's vocabulary reach the dashboard.

var (
	openaiOnce   sync.Once
	openaiClient openai.Client
)

func openaiAPI() *openai.Client {
	openaiOnce.Do(func() {
		var opts []option.RequestOption
		if key := os.Getenv("OPENAI_API_KEY"); key != "" {
			opts = append(opts, option.WithAPIKey(key))
		}
		// Honours OPENAI_BASE_URL, so this same path drives Azure OpenAI, a
		// local vLLM or Ollama server, or a gateway like LiteLLM. Worth knowing
		// before anyone concludes the demo needs an OpenAI account.
		if base := os.Getenv("OPENAI_BASE_URL"); base != "" {
			opts = append(opts, option.WithBaseURL(base))
		}
		openaiClient = openai.NewClient(opts...)
	})
	return &openaiClient
}

func invokeOpenAI(ctx context.Context, req LLMRequest) (LLMReply, error) {
	if os.Getenv("OPENAI_API_KEY") == "" && os.Getenv("OPENAI_BASE_URL") == "" {
		return LLMReply{}, fmt.Errorf("authentication: neither OPENAI_API_KEY nor OPENAI_BASE_URL is set")
	}

	params := openai.ChatCompletionNewParams{
		Model:    shared.ChatModel(req.Model),
		Messages: toOpenAIMessages(req.System, req.Messages),
	}
	if req.MaxTokens > 0 {
		// max_completion_tokens, not the deprecated max_tokens: the newer
		// reasoning models reject max_tokens outright, and it excluded
		// reasoning tokens from the cap even where it was accepted.
		params.MaxCompletionTokens = openai.Int(req.MaxTokens)
	}
	if req.Effort != "" {
		params.ReasoningEffort = shared.ReasoningEffort(req.Effort)
	}

	for _, t := range req.Tools {
		params.Tools = append(params.Tools, openai.ChatCompletionFunctionTool(shared.FunctionDefinitionParam{
			Name:        t.Name,
			Description: openai.String(t.Description),
			Parameters:  shared.FunctionParameters(t.Schema),
		}))
	}

	resp, err := openaiAPI().Chat.Completions.New(ctx, params)
	if err != nil {
		return LLMReply{}, err
	}
	if len(resp.Choices) == 0 {
		return LLMReply{}, fmt.Errorf("bad_request: response contained no choices")
	}

	choice := resp.Choices[0]
	out := LLMReply{
		Model:      resp.Model,
		Text:       choice.Message.Content,
		StopReason: choice.FinishReason,
		Usage: TokenUsage{
			InputTokens:       resp.Usage.PromptTokens,
			OutputTokens:      resp.Usage.CompletionTokens,
			ReasoningTokens:   resp.Usage.CompletionTokensDetails.ReasoningTokens,
			CachedInputTokens: resp.Usage.PromptTokensDetails.CachedTokens,
		},
	}

	// finish_reason "content_filter" is this API's refusal signal and, like
	// Anthropic's, it arrives on a 200. Mapping it into the same "refusal"
	// bucket is what lets one alert cover both providers.
	if choice.FinishReason == "content_filter" {
		return out, fmt.Errorf("refusal: response stopped by content filter")
	}

	for _, tc := range choice.Message.ToolCalls {
		// Arguments is already a JSON string here — no re-marshalling, which
		// keeps the bytes stable through Workflow history.
		out.ToolCalls = append(out.ToolCalls, ToolCall{
			ID:        tc.ID,
			Name:      tc.Function.Name,
			Arguments: tc.Function.Arguments,
		})
	}
	return out, nil
}

func toOpenAIMessages(system string, msgs []ChatMessage) []openai.ChatCompletionMessageParamUnion {
	out := make([]openai.ChatCompletionMessageParamUnion, 0, len(msgs)+1)
	if system != "" {
		// The system prompt is the first MESSAGE here, not a separate request
		// field. It is also the stable prefix that prompt caching keys on, so
		// anything volatile placed in front of it silently disables caching.
		out = append(out, openai.SystemMessage(system))
	}

	for _, m := range msgs {
		switch m.Role {
		case RoleUser:
			out = append(out, openai.UserMessage(m.Text))

		case RoleTool:
			out = append(out, openai.ToolMessage(m.Text, m.ToolCallID))

		case RoleAssistant:
			asst := openai.ChatCompletionAssistantMessageParam{}
			if m.Text != "" {
				asst.Content.OfString = openai.String(m.Text)
			}
			for _, tc := range m.ToolCalls {
				fn := openai.ChatCompletionMessageFunctionToolCallParam{
					ID: tc.ID,
					Function: openai.ChatCompletionMessageFunctionToolCallFunctionParam{
						Name:      tc.Name,
						Arguments: tc.Arguments,
					},
				}
				asst.ToolCalls = append(asst.ToolCalls,
					openai.ChatCompletionMessageToolCallUnionParam{OfFunction: &fn})
			}
			out = append(out, openai.ChatCompletionMessageParamUnion{OfAssistant: &asst})
		}
	}
	return out
}
