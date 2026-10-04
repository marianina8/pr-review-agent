package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// Model is anything that can take a conversation and reply, possibly asking to run tools.
// claude.go, ollama.go and bedrock.go each translate these plain types to and from their own API.
type Model interface {
	Chat(ctx context.Context, system string, messages []Message, tools []Tool) (Response, error)
}

// Message is one turn of the conversation.
type Message struct {
	Role        string       // "user" or "assistant"
	Text        string       // what was said (can be empty when the turn is only tool calls or results)
	ToolCalls   []ToolCall   // assistant turns: tools the model wants to run
	ToolResults []ToolResult // user turns: what those tools returned
}

// ToolCall is the model asking to run one tool.
type ToolCall struct {
	ID    string          // set by the model's API; ties the result back to the call
	Name  string          // which tool
	Input json.RawMessage // the tool's arguments, as a JSON object
}

// ToolResult is what we send back after running a tool.
type ToolResult struct {
	CallID  string
	Name    string
	Output  string
	IsError bool
}

// Tool describes a tool to the model: its name, when to use it, and its arguments as JSON schema.
type Tool struct {
	Name        string
	Description string
	Schema      map[string]any
}

// Response is the model's reply plus how many tokens it cost.
type Response struct {
	Message Message
	Usage   Usage
}

// Usage counts tokens.
type Usage struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
}

func (u *Usage) Add(other Usage) {
	u.InputTokens += other.InputTokens
	u.OutputTokens += other.OutputTokens
}

// postJSON sends body as JSON and decodes the JSON reply into out.
// It retries a few times when the server is busy (429 or 5xx), which happens with hosted models.
func postJSON(ctx context.Context, client *http.Client, url string, headers map[string]string, body, out any) error {
	payload, err := json.Marshal(body)
	if err != nil {
		return err
	}

	const attempts = 4
	for attempt := 1; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(payload))
		if err != nil {
			return err
		}
		req.Header.Set("content-type", "application/json")
		for name, value := range headers {
			req.Header.Set(name, value)
		}

		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		data, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return err
		}

		if resp.StatusCode == http.StatusOK {
			return json.Unmarshal(data, out)
		}
		busy := resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500
		if !busy || attempt == attempts {
			return fmt.Errorf("HTTP %d: %s", resp.StatusCode, shorten(string(data), 1000))
		}

		wait := time.Duration(attempt*attempt) * 5 * time.Second // 5s, 20s, 45s
		select {
		case <-time.After(wait):
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// shorten cuts s to at most n bytes, for error messages.
func shorten(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
