package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"
)

// Ollama talks to a local (or self-hosted) Ollama server's /api/chat endpoint.
type Ollama struct {
	ModelName string
	MaxTokens int
	// ContextTokens is Ollama's num_ctx. Ollama's default is small (a few thousand tokens) and it
	// silently drops the start of anything longer, which is where the instructions are.
	ContextTokens int
	host          string
	http          *http.Client
}

func NewOllama(modelName string, maxTokens, contextTokens int) *Ollama {
	host := os.Getenv("OLLAMA_HOST")
	if host == "" {
		host = "http://localhost:11434"
	}
	if !strings.HasPrefix(host, "http") {
		host = "http://" + host
	}
	return &Ollama{
		ModelName:     modelName,
		MaxTokens:     maxTokens,
		ContextTokens: contextTokens,
		host:          strings.TrimRight(host, "/"),
		http:          &http.Client{Timeout: 30 * time.Minute}, // CPU-only machines are slow
	}
}

// The request and response shapes of POST /api/chat (only the fields we use).

type ollamaRequest struct {
	Model    string          `json:"model"`
	Messages []ollamaMessage `json:"messages"`
	Tools    []ollamaTool    `json:"tools,omitempty"`
	Stream   bool            `json:"stream"`
	Options  map[string]any  `json:"options"`
}

type ollamaMessage struct {
	Role      string           `json:"role"` // system, user, assistant or tool
	Content   string           `json:"content"`
	ToolCalls []ollamaToolCall `json:"tool_calls,omitempty"`
	ToolName  string           `json:"tool_name,omitempty"` // on tool results
}

type ollamaToolCall struct {
	Function struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	} `json:"function"`
}

type ollamaTool struct {
	Type     string `json:"type"`
	Function struct {
		Name        string         `json:"name"`
		Description string         `json:"description"`
		Parameters  map[string]any `json:"parameters"`
	} `json:"function"`
}

type ollamaResponse struct {
	Message         ollamaMessage `json:"message"`
	PromptEvalCount int           `json:"prompt_eval_count"`
	EvalCount       int           `json:"eval_count"`
}

func (o *Ollama) Chat(ctx context.Context, system string, messages []Message, tools []Tool) (Response, error) {
	req := ollamaRequest{
		Model:   o.ModelName,
		Stream:  false,
		Options: map[string]any{"num_ctx": o.ContextTokens, "num_predict": o.MaxTokens},
	}
	for _, tool := range tools {
		var t ollamaTool
		t.Type = "function"
		t.Function.Name = tool.Name
		t.Function.Description = tool.Description
		t.Function.Parameters = tool.Schema
		req.Tools = append(req.Tools, t)
	}

	req.Messages = append(req.Messages, ollamaMessage{Role: "system", Content: system})
	for _, msg := range messages {
		// Tool results are separate "tool" messages in Ollama.
		for _, result := range msg.ToolResults {
			req.Messages = append(req.Messages, ollamaMessage{Role: "tool", Content: result.Output, ToolName: result.Name})
		}
		if msg.Text == "" && len(msg.ToolCalls) == 0 {
			continue
		}
		out := ollamaMessage{Role: msg.Role, Content: msg.Text}
		for _, call := range msg.ToolCalls {
			var c ollamaToolCall
			c.Function.Name = call.Name
			c.Function.Arguments = call.Input
			out.ToolCalls = append(out.ToolCalls, c)
		}
		req.Messages = append(req.Messages, out)
	}

	var resp ollamaResponse
	if err := postJSON(ctx, o.http, o.host+"/api/chat", nil, req, &resp); err != nil {
		if strings.Contains(err.Error(), "does not support tools") {
			return Response{}, fmt.Errorf("ollama: model %q does not support tool calling; pick one that does (for example qwen2.5-coder:7b)", o.ModelName)
		}
		return Response{}, fmt.Errorf("ollama: %w", err)
	}

	reply := Message{Role: "assistant", Text: resp.Message.Content}
	for i, c := range resp.Message.ToolCalls {
		// Ollama doesn't give calls an ID, so number them.
		reply.ToolCalls = append(reply.ToolCalls, ToolCall{
			ID:    fmt.Sprintf("call_%d", i+1),
			Name:  c.Function.Name,
			Input: c.Function.Arguments,
		})
	}
	return Response{Message: reply, Usage: Usage{InputTokens: resp.PromptEvalCount, OutputTokens: resp.EvalCount}}, nil
}
