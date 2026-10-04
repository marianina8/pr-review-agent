package main

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/document"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
)

// Bedrock talks to Amazon Bedrock's Converse API, which gives every Bedrock model
// (Qwen, DeepSeek, Claude, ...) the same request shape. AWS credentials come from the
// usual places: environment, ~/.aws, or the role GitHub Actions signs in to.
type Bedrock struct {
	ModelID   string
	MaxTokens int
	client    *bedrockruntime.Client
}

func NewBedrock(ctx context.Context, region, modelID string, maxTokens int) (*Bedrock, error) {
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion(region), config.WithRetryMaxAttempts(6))
	if err != nil {
		return nil, fmt.Errorf("load AWS config: %w", err)
	}
	return &Bedrock{ModelID: modelID, MaxTokens: maxTokens, client: bedrockruntime.NewFromConfig(cfg)}, nil
}

func (b *Bedrock) Chat(ctx context.Context, system string, messages []Message, tools []Tool) (Response, error) {
	input := &bedrockruntime.ConverseInput{
		ModelId:         aws.String(b.ModelID),
		System:          []types.SystemContentBlock{&types.SystemContentBlockMemberText{Value: system}},
		InferenceConfig: &types.InferenceConfiguration{MaxTokens: aws.Int32(int32(b.MaxTokens))},
	}
	if len(tools) > 0 {
		input.ToolConfig = &types.ToolConfiguration{}
		for _, tool := range tools {
			input.ToolConfig.Tools = append(input.ToolConfig.Tools, &types.ToolMemberToolSpec{Value: types.ToolSpecification{
				Name:        aws.String(tool.Name),
				Description: aws.String(tool.Description),
				InputSchema: &types.ToolInputSchemaMemberJson{Value: document.NewLazyDocument(tool.Schema)},
			}})
		}
	}
	for _, msg := range messages {
		input.Messages = append(input.Messages, toBedrockMessage(msg))
	}

	out, err := b.client.Converse(ctx, input)
	if err != nil {
		return Response{}, fmt.Errorf("bedrock: %w", err)
	}

	reply := Message{Role: "assistant"}
	if msg, ok := out.Output.(*types.ConverseOutputMemberMessage); ok {
		for _, block := range msg.Value.Content {
			switch block := block.(type) {
			case *types.ContentBlockMemberText:
				reply.Text += block.Value
			case *types.ContentBlockMemberToolUse:
				var args map[string]any
				if err := block.Value.Input.UnmarshalSmithyDocument(&args); err != nil {
					return Response{}, fmt.Errorf("bedrock: read tool arguments: %w", err)
				}
				raw, _ := json.Marshal(args)
				reply.ToolCalls = append(reply.ToolCalls, ToolCall{
					ID:    aws.ToString(block.Value.ToolUseId),
					Name:  aws.ToString(block.Value.Name),
					Input: raw,
				})
			}
		}
	}

	var usage Usage
	if out.Usage != nil {
		usage = Usage{InputTokens: int(aws.ToInt32(out.Usage.InputTokens)), OutputTokens: int(aws.ToInt32(out.Usage.OutputTokens))}
	}
	return Response{Message: reply, Usage: usage}, nil
}

// toBedrockMessage turns one of our messages into Converse content blocks.
func toBedrockMessage(msg Message) types.Message {
	out := types.Message{Role: types.ConversationRole(msg.Role)}
	for _, result := range msg.ToolResults {
		status := types.ToolResultStatusSuccess
		if result.IsError {
			status = types.ToolResultStatusError
		}
		out.Content = append(out.Content, &types.ContentBlockMemberToolResult{Value: types.ToolResultBlock{
			ToolUseId: aws.String(result.CallID),
			Content:   []types.ToolResultContentBlock{&types.ToolResultContentBlockMemberText{Value: result.Output}},
			Status:    status,
		}})
	}
	if msg.Text != "" {
		out.Content = append(out.Content, &types.ContentBlockMemberText{Value: msg.Text})
	}
	for _, call := range msg.ToolCalls {
		var args map[string]any
		if err := json.Unmarshal(call.Input, &args); err != nil || args == nil {
			args = map[string]any{}
		}
		out.Content = append(out.Content, &types.ContentBlockMemberToolUse{Value: types.ToolUseBlock{
			ToolUseId: aws.String(call.ID),
			Name:      aws.String(call.Name),
			Input:     document.NewLazyDocument(args),
		}})
	}
	return out
}
