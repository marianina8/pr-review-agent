package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeModel replays scripted replies and remembers what it was sent.
type fakeModel struct {
	replies []Message
	seen    [][]Message
}

func (f *fakeModel) Chat(ctx context.Context, system string, messages []Message, tools []Tool) (Response, error) {
	f.seen = append(f.seen, append([]Message(nil), messages...))
	reply := f.replies[0]
	f.replies = f.replies[1:]
	return Response{Message: reply, Usage: Usage{InputTokens: 10, OutputTokens: 5}}, nil
}

func newTestRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	write(t, dir, "go.mod", "module example.com/demo\n\ngo 1.22\n")
	write(t, dir, "main.go", "package main\n\nfunc main() {}\n\nfunc double(n int) int { return n * 2 }\n")
	return dir
}

func write(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func newTestToolbox(t *testing.T, dir string) *Toolbox {
	t.Helper()
	tools, err := NewToolbox(dir, 20000, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	return tools
}

func TestLoopRunsToolThenReturnsReview(t *testing.T) {
	tools := newTestToolbox(t, newTestRepo(t))
	model := &fakeModel{replies: []Message{
		{Role: "assistant", ToolCalls: []ToolCall{{ID: "1", Name: "read_file", Input: json.RawMessage(`{"path":"main.go"}`)}}},
		{Role: "assistant", Text: "### Error handling\n- none found"},
	}}
	agent := &Agent{Model: model, Tools: tools, MaxSteps: 5, Log: io.Discard}

	result, err := agent.Run(context.Background(), "review it")
	if err != nil {
		t.Fatal(err)
	}
	if result.Review != "### Error handling\n- none found" {
		t.Errorf("review = %q", result.Review)
	}
	if result.Steps != 2 || result.ToolCalls != 1 || result.Usage.InputTokens != 20 {
		t.Errorf("result = %+v", result)
	}
	// The second call must carry the tool's output back to the model.
	last := model.seen[1][len(model.seen[1])-1]
	if len(last.ToolResults) != 1 || !strings.Contains(last.ToolResults[0].Output, "func double") {
		t.Errorf("tool result not sent back: %+v", last)
	}
}

func TestLoopStopsAtMaxSteps(t *testing.T) {
	tools := newTestToolbox(t, newTestRepo(t))
	call := Message{Role: "assistant", ToolCalls: []ToolCall{{ID: "1", Name: "list_files", Input: json.RawMessage(`{}`)}}}
	model := &fakeModel{replies: []Message{call, call}}
	agent := &Agent{Model: model, Tools: tools, MaxSteps: 2, Log: io.Discard}

	if _, err := agent.Run(context.Background(), "review it"); err == nil {
		t.Fatal("expected an error after running out of steps")
	}
	// Before the last call, the model is told to stop calling tools.
	if note := model.seen[1][len(model.seen[1])-1].Text; !strings.Contains(note, "last step") {
		t.Errorf("last-step note missing, got %q", note)
	}
}

func TestPathsOutsideRepoAreRejected(t *testing.T) {
	tools := newTestToolbox(t, newTestRepo(t))
	for _, input := range []string{`{"path":"../secret.txt"}`, `{"path":"/etc/passwd"}`} {
		result := tools.Run(context.Background(), ToolCall{Name: "read_file", Input: json.RawMessage(input)})
		if !result.IsError {
			t.Errorf("%s: expected an error, got %q", input, result.Output)
		}
	}
	for _, packages := range []string{"-exec=sh", "../...", "example.com/x@latest", "./../x"} {
		if _, err := packagePatterns(packages); err == nil {
			t.Errorf("packagePatterns(%q) should fail", packages)
		}
	}
}

func TestScratchTestsAreLimitedAndCleanedUp(t *testing.T) {
	dir := newTestRepo(t)
	tools := newTestToolbox(t, dir)
	run := func(name, input string) ToolResult {
		return tools.Run(context.Background(), ToolCall{Name: name, Input: json.RawMessage(input)})
	}

	if r := run("write_scratch_test", `{"path":"main.go","content":"package main"}`); !r.IsError {
		t.Error("overwrote main.go")
	}
	test := `package main\n\nimport \"testing\"\n\nfunc TestDouble(t *testing.T) { if double(2) != 4 { t.Fatal(\"bad\") } }\n`
	if r := run("write_scratch_test", `{"path":"zz_review_double_test.go","content":"`+test+`"}`); r.IsError {
		t.Fatalf("write failed: %s", r.Output)
	}
	if r := run("go_test", `{"run":"TestDouble","verbose":true}`); !strings.Contains(r.Output, "--- PASS: TestDouble") {
		t.Errorf("go test output: %s", r.Output)
	}

	tools.Cleanup()
	if _, err := os.Stat(filepath.Join(dir, "zz_review_double_test.go")); !os.IsNotExist(err) {
		t.Error("scratch test was not deleted")
	}
}

func TestCommandsGetNoSecrets(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "secret")
	for _, kv := range cleanEnv() {
		if strings.HasPrefix(kv, "ANTHROPIC_") || strings.HasPrefix(kv, "AWS_") {
			t.Errorf("leaked %s", kv)
		}
	}
}

// The model files are tested against fake servers that check the request and send a canned reply.

func TestClaudeTranslatesToolCalls(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("x-api-key") != "sk-test" || r.URL.Path != "/v1/messages" {
			t.Errorf("bad request: %s %v", r.URL.Path, r.Header)
		}
		var req claudeRequest
		json.NewDecoder(r.Body).Decode(&req)
		if len(req.Tools) == 0 || req.System == "" || req.Messages[1].Content[0].Type != "tool_use" || req.Messages[2].Content[0].Type != "tool_result" {
			t.Errorf("bad body: %+v", req)
		}
		io.WriteString(w, `{"content":[{"type":"text","text":"checking"},{"type":"tool_use","id":"t2","name":"go_vet","input":{}}],"usage":{"input_tokens":7,"output_tokens":3}}`)
	}))
	defer server.Close()
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("ANTHROPIC_BASE_URL", server.URL)

	claude, _ := NewClaude("claude-test", 100)
	resp, err := claude.Chat(context.Background(), "rules", conversationSoFar(), testTools())
	if err != nil {
		t.Fatal(err)
	}
	checkReply(t, resp, "t2")
}

func TestOllamaTranslatesToolCalls(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req ollamaRequest
		json.NewDecoder(r.Body).Decode(&req)
		roles := []string{}
		for _, m := range req.Messages {
			roles = append(roles, m.Role)
		}
		if strings.Join(roles, ",") != "system,user,assistant,tool,user" || req.Options["num_ctx"] == nil || req.Stream {
			t.Errorf("bad body: roles %v, options %v", roles, req.Options)
		}
		io.WriteString(w, `{"message":{"role":"assistant","content":"checking","tool_calls":[{"function":{"name":"go_vet","arguments":{}}}]},"prompt_eval_count":7,"eval_count":3}`)
	}))
	defer server.Close()
	t.Setenv("OLLAMA_HOST", server.URL)

	resp, err := NewOllama("qwen-test", 100, 8192).Chat(context.Background(), "rules", conversationSoFar(), testTools())
	if err != nil {
		t.Fatal(err)
	}
	checkReply(t, resp, "call_1")
}

func TestBedrockTranslatesToolCalls(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.URL.Path != "/model/test-model/converse" || !strings.Contains(string(body), `"toolUse"`) || !strings.Contains(string(body), `"toolResult"`) {
			t.Errorf("bad request: %s %s", r.URL.Path, body)
		}
		w.Header().Set("content-type", "application/json")
		io.WriteString(w, `{"output":{"message":{"role":"assistant","content":[{"text":"checking"},{"toolUse":{"toolUseId":"b2","name":"go_vet","input":{}}}]}},"stopReason":"tool_use","usage":{"inputTokens":7,"outputTokens":3,"totalTokens":10},"metrics":{"latencyMs":1}}`)
	}))
	defer server.Close()
	t.Setenv("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", server.URL)
	t.Setenv("AWS_ACCESS_KEY_ID", "test")
	t.Setenv("AWS_SECRET_ACCESS_KEY", "test")

	bedrock, err := NewBedrock(context.Background(), "us-west-2", "test-model", 100)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := bedrock.Chat(context.Background(), "rules", conversationSoFar(), testTools())
	if err != nil {
		t.Fatal(err)
	}
	checkReply(t, resp, "b2")
}

// conversationSoFar is a task, one tool call, its result, and a steps-left note.
func conversationSoFar() []Message {
	return []Message{
		{Role: "user", Text: "review it"},
		{Role: "assistant", ToolCalls: []ToolCall{{ID: "t1", Name: "list_files", Input: json.RawMessage(`{"path":"."}`)}}},
		{Role: "user", Text: "(3 steps left)", ToolResults: []ToolResult{{CallID: "t1", Name: "list_files", Output: "main.go (10 bytes)"}}},
	}
}

func testTools() []Tool {
	return []Tool{{Name: "go_vet", Description: "Run go vet.", Schema: map[string]any{"type": "object", "properties": map[string]any{}}}}
}

func checkReply(t *testing.T, resp Response, wantID string) {
	t.Helper()
	if resp.Message.Text != "checking" || len(resp.Message.ToolCalls) != 1 || resp.Message.ToolCalls[0].Name != "go_vet" || resp.Message.ToolCalls[0].ID != wantID {
		t.Errorf("reply = %+v", resp.Message)
	}
	if resp.Usage != (Usage{InputTokens: 7, OutputTokens: 3}) {
		t.Errorf("usage = %+v", resp.Usage)
	}
}
