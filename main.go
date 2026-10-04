// pr-review-agent reviews Go code with an AI model that can read the repository and run
// go vet, go test and its own scratch tests before it writes the review.
//
//	pr-review-agent --model claude                      # review everything under .
//	pr-review-agent --model ollama --target ./internal  # one folder, with a local model
//	pr-review-agent --model bedrock --mode pr --base origin/main
//
// The review goes to stdout; progress goes to stderr.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"
)

var defaultModelNames = map[string]string{
	"claude":  "claude-sonnet-5-5",
	"ollama":  "qwen2.5-coder:7b",
	"bedrock": "qwen.qwen3-coder-480b-a35b-v1:0",
}

func main() {
	modelFlag := flag.String("model", "claude", "which model API to use: claude, ollama or bedrock")
	modelName := flag.String("model-name", "", "model to ask (default depends on --model, e.g. claude-sonnet-5-5)")
	mode := flag.String("mode", "code", "code: review everything under --target; pr: review changes since --base")
	target := flag.String("target", ".", "code mode: file or folder to review, relative to --repo")
	base := flag.String("base", "origin/main", "pr mode: branch or commit to compare with")
	repo := flag.String("repo", ".", "repository root; the tools can't see outside it")
	maxSteps := flag.Int("max-steps", 20, "max model calls before giving up")
	maxTokens := flag.Int("max-tokens", 4096, "max tokens the model may write per call")
	maxOutput := flag.Int("max-output", 20000, "max bytes of any one tool result sent to the model")
	toolTimeout := flag.Duration("tool-timeout", 2*time.Minute, "max time for any one command (go test, go vet, git)")
	region := flag.String("region", envOr("AWS_REGION", "us-west-2"), "bedrock: AWS region")
	ollamaContext := flag.Int("ollama-context", 32768, "ollama: context window in tokens (num_ctx)")
	statsFile := flag.String("stats", "", "also write steps, tool calls, tokens and time to this JSON file")
	flag.Parse()

	if *modelName == "" {
		*modelName = defaultModelNames[*modelFlag]
	}
	if *mode != "code" && *mode != "pr" {
		fail(2, "--mode must be code or pr")
	}

	ctx := context.Background()

	var model Model
	switch *modelFlag {
	case "claude":
		claude, err := NewClaude(*modelName, *maxTokens)
		if err != nil {
			fail(1, err.Error())
		}
		model = claude
	case "ollama":
		model = NewOllama(*modelName, *maxTokens, *ollamaContext)
	case "bedrock":
		bedrock, err := NewBedrock(ctx, *region, *modelName, *maxTokens)
		if err != nil {
			fail(1, err.Error())
		}
		model = bedrock
	default:
		fail(2, "--model must be claude, ollama or bedrock")
	}

	tools, err := NewToolbox(*repo, *maxOutput, *toolTimeout)
	if err != nil {
		fail(1, err.Error())
	}
	defer tools.Cleanup() // delete any scratch tests the model wrote

	task, err := describeTask(tools, *mode, *target, *base)
	if err != nil {
		fail(1, err.Error())
	}

	agent := &Agent{Model: model, Tools: tools, MaxSteps: *maxSteps, Log: os.Stderr}
	fmt.Fprintf(os.Stderr, "reviewing with %s (%s), mode %s, up to %d steps\n", *modelFlag, *modelName, *mode, *maxSteps)

	started := time.Now()
	result, err := agent.Run(ctx, task)
	seconds := time.Since(started).Seconds()
	fmt.Fprintf(os.Stderr, "done: %d steps, %d tool calls, %d input + %d output tokens, %.0fs\n",
		result.Steps, result.ToolCalls, result.Usage.InputTokens, result.Usage.OutputTokens, seconds)

	if *statsFile != "" {
		writeStats(*statsFile, *modelFlag, *modelName, result, seconds, err)
	}
	if err != nil {
		tools.Cleanup() // fail exits without running deferred calls
		fail(1, err.Error())
	}
	fmt.Println(result.Review)
}

// describeTask is the first message the model sees.
func describeTask(tools *Toolbox, mode, target, base string) (string, error) {
	if mode == "pr" {
		return fmt.Sprintf("Review the changes on this branch compared with %s. Start with git_diff (base %q), "+
			"then read the surrounding code. Only report problems in the changed code or caused by it.", base, base), nil
	}
	files, err := tools.listFiles(target)
	if err != nil {
		return "", fmt.Errorf("--target: %w", err)
	}
	return fmt.Sprintf("Review the Go code in %s. These are the files:\n\n%s", target, files), nil
}

func writeStats(path, provider, modelName string, result Result, seconds float64, runErr error) {
	stats := map[string]any{
		"provider":   provider,
		"model":      modelName,
		"steps":      result.Steps,
		"tool_calls": result.ToolCalls,
		"usage":      result.Usage,
		"seconds":    int(seconds),
	}
	if runErr != nil {
		stats["error"] = runErr.Error()
	}
	data, _ := json.MarshalIndent(stats, "", "  ")
	if err := os.WriteFile(path, data, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "could not write %s: %v\n", path, err)
	}
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func fail(code int, msg string) {
	fmt.Fprintln(os.Stderr, "pr-review-agent:", msg)
	os.Exit(code)
}
