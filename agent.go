package main

import (
	"context"
	_ "embed"
	"fmt"
	"io"
	"strings"
)

// The reviewer's rules ship inside the binary. Edit guidelines.md and rebuild to change them.
//
//go:embed guidelines.md
var guidelines string

// Agent is the loop: ask the model, run the tools it asks for, send back the results, repeat
// until it answers without asking for a tool.
type Agent struct {
	Model    Model
	Tools    *Toolbox
	MaxSteps int
	Log      io.Writer // progress goes here (stderr), so the review on stdout can be piped
}

// Result is the final review plus what it took to get there.
type Result struct {
	Review    string `json:"-"`
	Steps     int    `json:"steps"`      // model calls
	ToolCalls int    `json:"tool_calls"` // tools run
	Usage     Usage  `json:"usage"`
}

func (a *Agent) Run(ctx context.Context, task string) (Result, error) {
	messages := []Message{{Role: "user", Text: task}}
	var result Result

	for step := 1; step <= a.MaxSteps; step++ {
		resp, err := a.Model.Chat(ctx, guidelines, messages, a.Tools.Tools())
		if err != nil {
			return result, fmt.Errorf("model call (step %d): %w", step, err)
		}
		result.Steps = step
		result.Usage.Add(resp.Usage)
		messages = append(messages, resp.Message)

		// No tool calls means the model is done: its text is the review.
		if len(resp.Message.ToolCalls) == 0 {
			result.Review = resp.Message.Text
			return result, nil
		}

		// Run every tool it asked for and send all the results back in one message.
		reply := Message{Role: "user"}
		for _, call := range resp.Message.ToolCalls {
			fmt.Fprintf(a.Log, "step %d: %s %s\n", step, call.Name, call.Input)
			toolResult := a.Tools.Run(ctx, call)
			fmt.Fprintf(a.Log, "%s\n", indent(firstLines(toolResult.Output, 6)))
			reply.ToolResults = append(reply.ToolResults, toolResult)
			result.ToolCalls++
		}
		reply.Text = stepsLeftNote(a.MaxSteps - step)
		messages = append(messages, reply)
	}
	return result, fmt.Errorf("stopped after %d steps without a final answer", a.MaxSteps)
}

// stepsLeftNote tells the model how much budget it has, so it finishes in time.
func stepsLeftNote(left int) string {
	if left == 1 {
		return "This is your last step. Do not call any more tools: write the final review now."
	}
	return fmt.Sprintf("(%d steps left)", left)
}

func firstLines(s string, n int) string {
	lines := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(lines) > n {
		lines = append(lines[:n], fmt.Sprintf("... (%d more lines)", len(lines)-n))
	}
	return strings.Join(lines, "\n")
}

func indent(s string) string {
	return "    " + strings.ReplaceAll(s, "\n", "\n    ")
}
