package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"
)

// Claude talks to the Anthropic Messages API with plain net/http.
// The API key comes from ANTHROPIC_API_KEY and is only ever put in a request header.
type Claude struct {
	ModelName string
	MaxTokens int
	apiKey    string
	baseURL   string
	http      *http.Client
}

func NewClaude(modelName string, maxTokens int) (*Claude, error) {
	key := os.Getenv("ANTHROPIC_API_KEY")
	if key == "" {
		return nil, fmt.Errorf("ANTHROPIC_API_KEY is not set")
	}
	baseURL := os.Getenv("ANTHROPIC_BASE_URL") // only set in tests
	if baseURL == "" {
		baseURL = "https://api.anthropic.com"
	}
	return &Claude{
		ModelName: modelName,
		MaxTokens: maxTokens,
		apiKey:    key,
		baseURL:   baseURL,
		http:      &http.Client{Timeout: 10 * time.Minute},
	}, nil
}

// The request and response shapes of POST /v1/messages (only the fields we use).

type claudeRequest struct {
	Model     string          `json:"model"`
	MaxTokens int             `json:"max_tokens"`
	System    string          `json:"system"`
	Messages  []claudeMessage `json:"messages"`
	Tools     []claudeTool    `json:"tools,omitempty"`
}

type claudeMessage struct {
	Role    string        `json:"role"`
	Content []claudeBlock `json:"content"`
}

// claudeBlock is one piece of a message: text, a tool call (tool_use) or a tool result.
type claudeBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text,omitempty"`
	ID        string          `json:"id,omitempty"`
	Name      string          `json:"name,omitempty"`
	Input     json.RawMessage `json:"input,omitempty"`
	ToolUseID string          `json:"tool_use_id,omitempty"`
	Content   string          `json:"content,omitempty"`
	IsError   bool            `json:"is_error,omitempty"`
}

type claudeTool struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"input_schema"`
}

type claudeResponse struct {
	Content []claudeBlock `json:"content"`
	Usage   Usage         `json:"usage"`
}

func (c *Claude) Chat(ctx context.Context, system string, messages []Message, tools []Tool) (Response, error) {
	req := claudeRequest{
		Model:     c.ModelName,
		MaxTokens: c.MaxTokens,
		System:    system,
	}
	for _, tool := range tools {
		req.Tools = append(req.Tools, claudeTool{tool.Name, tool.Description, tool.Schema})
	}
	for _, msg := range messages {
		req.Messages = append(req.Messages, toClaudeMessage(msg))
	}

	headers := map[string]string{
		"x-api-key":         c.apiKey,
		"anthropic-version": "2023-06-01",
	}
	var resp claudeResponse
	if err := postJSON(ctx, c.http, c.baseURL+"/v1/messages", headers, req, &resp); err != nil {
		return Response{}, fmt.Errorf("anthropic: %w", err)
	}

	reply := Message{Role: "assistant"}
	for _, block := range resp.Content {
		switch block.Type {
		case "text":
			reply.Text += block.Text
		case "tool_use":
			reply.ToolCalls = append(reply.ToolCalls, ToolCall{ID: block.ID, Name: block.Name, Input: block.Input})
		}
	}
	return Response{Message: reply, Usage: resp.Usage}, nil
}

// toClaudeMessage turns one of our messages into content blocks.
// Tool results go first: the API wants them right after the assistant's tool calls.
func toClaudeMessage(msg Message) claudeMessage {
	out := claudeMessage{Role: msg.Role}
	for _, result := range msg.ToolResults {
		out.Content = append(out.Content, claudeBlock{
			Type: "tool_result", ToolUseID: result.CallID, Content: result.Output, IsError: result.IsError,
		})
	}
	if msg.Text != "" {
		out.Content = append(out.Content, claudeBlock{Type: "text", Text: msg.Text})
	}
	for _, call := range msg.ToolCalls {
		out.Content = append(out.Content, claudeBlock{Type: "tool_use", ID: call.ID, Name: call.Name, Input: call.Input})
	}
	return out
}
