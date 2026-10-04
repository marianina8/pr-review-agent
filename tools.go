package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"time"
)

// Toolbox holds the tools the model can call. Every tool is confined to one repository folder.
//
// Guardrails:
//   - Read-only, except write_scratch_test, which may only create zz_review_*_test.go files.
//     Those files are deleted by Cleanup when the review ends.
//   - No shell. Each tool runs one fixed command (git or go) with its arguments as a slice.
//   - Go and git run with a stripped-down environment: no API keys, no AWS or GitHub
//     credentials, and GOPROXY=off so nothing is downloaded.
//   - Every command has a timeout, and big outputs are cut down before the model sees them.
//
// Limits: a scratch test is real Go code. It runs as your user and can reach the network,
// so only review code you trust, and don't leave credentials in files inside the checkout.
type Toolbox struct {
	root      string        // absolute path of the repository
	maxOutput int           // max bytes of any one tool result
	timeout   time.Duration // max time for any one command
	scratch   []string      // scratch test files we created, to delete later
}

func NewToolbox(root string, maxOutput int, timeout time.Duration) (*Toolbox, error) {
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	abs, err = filepath.EvalSymlinks(abs)
	if err != nil {
		return nil, err
	}
	return &Toolbox{root: abs, maxOutput: maxOutput, timeout: timeout}, nil
}

// Tools lists the tools for the model. Descriptions are written for the model to read.
func (tb *Toolbox) Tools() []Tool {
	str := func(desc string) map[string]any { return map[string]any{"type": "string", "description": desc} }
	num := func(desc string) map[string]any { return map[string]any{"type": "integer", "description": desc} }
	schema := func(props map[string]any, required ...string) map[string]any {
		if required == nil {
			required = []string{}
		}
		return map[string]any{"type": "object", "properties": props, "required": required}
	}

	return []Tool{
		{"list_files", "List the files under a folder of the repository, with their sizes.",
			schema(map[string]any{"path": str("Folder relative to the repository root, e.g. \".\"")})},
		{"read_file", "Read a file from the repository. Lines are numbered so you can cite them.",
			schema(map[string]any{
				"path":       str("File relative to the repository root, e.g. \"main.go\""),
				"start_line": num("First line to read (default 1)"),
				"end_line":   num("Last line to read (default: end of file)"),
			}, "path")},
		{"grep", "Search the repository's .go files for a regular expression (Go syntax). Returns file:line: text.",
			schema(map[string]any{
				"pattern": str("Regular expression"),
				"path":    str("File or folder to search (default \".\")"),
			}, "pattern")},
		{"git_diff", "Show what changed compared with a base branch or commit (git diff base...HEAD).",
			schema(map[string]any{"base": str("Branch or commit to compare with, e.g. \"origin/main\"")}, "base")},
		{"go_vet", "Run go vet.",
			schema(map[string]any{"packages": str("Package patterns separated by spaces (default \"./...\")")})},
		{"go_test", "Run go test (no network). Use run to pick tests, e.g. one you wrote with write_scratch_test.",
			schema(map[string]any{
				"packages": str("Package patterns separated by spaces (default \"./...\")"),
				"run":      str("Only run tests matching this regular expression (go test -run)"),
				"verbose":  map[string]any{"type": "boolean", "description": "Pass -v to see t.Log output"},
			})},
		{"write_scratch_test", "Create a throwaway Go test file to check a suspicion, then run it with go_test. " +
			"The file name must look like zz_review_<name>_test.go, inside the package folder you are testing. " +
			"It can't overwrite existing files and is deleted after the review.",
			schema(map[string]any{
				"path":    str("e.g. \"zz_review_apikey_test.go\" or \"internal/foo/zz_review_parse_test.go\""),
				"content": str("Full Go source of the test file"),
			}, "path", "content")},
	}
}

// Run runs one tool call. Errors are returned to the model as the result, not raised.
func (tb *Toolbox) Run(ctx context.Context, call ToolCall) ToolResult {
	output, err := tb.dispatch(ctx, call)
	if err != nil {
		return ToolResult{CallID: call.ID, Name: call.Name, Output: "error: " + err.Error(), IsError: true}
	}
	return ToolResult{CallID: call.ID, Name: call.Name, Output: truncate(output, tb.maxOutput)}
}

// toolArgs is every argument any tool takes; each tool reads the ones it needs.
type toolArgs struct {
	Path      string `json:"path"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
	Pattern   string `json:"pattern"`
	Base      string `json:"base"`
	Packages  string `json:"packages"`
	Run       string `json:"run"`
	Verbose   bool   `json:"verbose"`
	Content   string `json:"content"`
}

func (tb *Toolbox) dispatch(ctx context.Context, call ToolCall) (string, error) {
	var args toolArgs
	if len(call.Input) > 0 {
		if err := json.Unmarshal(call.Input, &args); err != nil {
			return "", fmt.Errorf("bad arguments: %w", err)
		}
	}

	switch call.Name {
	case "list_files":
		return tb.listFiles(args.Path)
	case "read_file":
		return tb.readFile(args.Path, args.StartLine, args.EndLine)
	case "grep":
		return tb.grep(args.Pattern, args.Path)
	case "git_diff":
		if args.Base == "" || strings.HasPrefix(args.Base, "-") {
			return "", errors.New("base must be a branch or commit name")
		}
		return tb.command(ctx, "git", "diff", "--no-color", args.Base+"...HEAD", "--")
	case "go_vet":
		packages, err := packagePatterns(args.Packages)
		if err != nil {
			return "", err
		}
		return tb.command(ctx, "go", append([]string{"vet"}, packages...)...)
	case "go_test":
		packages, err := packagePatterns(args.Packages)
		if err != nil {
			return "", err
		}
		cmdArgs := []string{"test", "-count=1"}
		if args.Verbose {
			cmdArgs = append(cmdArgs, "-v")
		}
		if args.Run != "" {
			cmdArgs = append(cmdArgs, "-run", args.Run)
		}
		return tb.command(ctx, "go", append(cmdArgs, packages...)...)
	case "write_scratch_test":
		return tb.writeScratchTest(args.Path, args.Content)
	default:
		return "", fmt.Errorf("unknown tool %q", call.Name)
	}
}

// resolve turns a model-supplied path into an absolute path, refusing anything outside the repository.
func (tb *Toolbox) resolve(path string) (string, error) {
	if path == "" {
		path = "."
	}
	if filepath.IsAbs(path) {
		return "", fmt.Errorf("path %q must be relative to the repository root", path)
	}
	full := filepath.Join(tb.root, path)
	if real, err := filepath.EvalSymlinks(full); err == nil {
		full = real // follow symlinks so they can't point outside the repository
	}
	rel, err := filepath.Rel(tb.root, full)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("path %q is outside the repository", path)
	}
	return full, nil
}

func (tb *Toolbox) listFiles(path string) (string, error) {
	dir, err := tb.resolve(path)
	if err != nil {
		return "", err
	}
	var lines []string
	err = filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && p != dir && (strings.HasPrefix(d.Name(), ".") || d.Name() == "vendor") {
			return filepath.SkipDir
		}
		if d.IsDir() {
			return nil
		}
		if len(lines) == 300 {
			lines = append(lines, "... (more files not shown)")
			return filepath.SkipAll
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(tb.root, p)
		lines = append(lines, fmt.Sprintf("%s (%d bytes)", filepath.ToSlash(rel), info.Size()))
		return nil
	})
	if err != nil {
		return "", err
	}
	if len(lines) == 0 {
		return "(no files)", nil
	}
	return strings.Join(lines, "\n"), nil
}

func (tb *Toolbox) readFile(path string, start, end int) (string, error) {
	full, err := tb.resolve(path)
	if err != nil {
		return "", err
	}
	data, err := os.ReadFile(full)
	if err != nil {
		return "", err
	}
	lines := strings.Split(string(data), "\n")
	if start < 1 {
		start = 1
	}
	if end < 1 || end > len(lines) {
		end = len(lines)
	}
	if start > end {
		return "", fmt.Errorf("start_line %d is past the end of the file (%d lines)", start, len(lines))
	}
	var out strings.Builder
	for i := start; i <= end; i++ {
		fmt.Fprintf(&out, "%4d| %s\n", i, lines[i-1])
	}
	return out.String(), nil
}

func (tb *Toolbox) grep(pattern, path string) (string, error) {
	re, err := regexp.Compile(pattern)
	if err != nil {
		return "", fmt.Errorf("bad pattern: %w", err)
	}
	start, err := tb.resolve(path)
	if err != nil {
		return "", err
	}
	var hits []string
	err = filepath.WalkDir(start, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() && p != start && (strings.HasPrefix(d.Name(), ".") || d.Name() == "vendor") {
			return filepath.SkipDir
		}
		if d.IsDir() || !strings.HasSuffix(p, ".go") {
			return nil
		}
		f, err := os.Open(p)
		if err != nil {
			return err
		}
		defer f.Close()
		rel, _ := filepath.Rel(tb.root, p)
		scanner := bufio.NewScanner(f)
		scanner.Buffer(make([]byte, 1024*1024), 1024*1024)
		for n := 1; scanner.Scan(); n++ {
			if re.MatchString(scanner.Text()) {
				hits = append(hits, fmt.Sprintf("%s:%d: %s", filepath.ToSlash(rel), n, strings.TrimSpace(scanner.Text())))
				if len(hits) == 100 {
					hits = append(hits, "... (more matches not shown)")
					return filepath.SkipAll
				}
			}
		}
		return scanner.Err()
	})
	if err != nil {
		return "", err
	}
	if len(hits) == 0 {
		return "(no matches)", nil
	}
	return strings.Join(hits, "\n"), nil
}

var scratchName = regexp.MustCompile(`^zz_review_[A-Za-z0-9_]+_test\.go$`)

func (tb *Toolbox) writeScratchTest(path, content string) (string, error) {
	if !scratchName.MatchString(filepath.Base(path)) {
		return "", errors.New("the file name must look like zz_review_<name>_test.go")
	}
	if len(content) > 20000 {
		return "", errors.New("file too large (max 20000 bytes)")
	}
	full, err := tb.resolve(path)
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(full); err == nil && !tb.ownsScratch(full) {
		return "", errors.New("a file with that name already exists; pick another name")
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		return "", err
	}
	if !tb.ownsScratch(full) {
		tb.scratch = append(tb.scratch, full)
	}
	return fmt.Sprintf("wrote %s (%d bytes); run it with go_test", path, len(content)), nil
}

func (tb *Toolbox) ownsScratch(full string) bool {
	return slices.Contains(tb.scratch, full)
}

// Cleanup deletes the scratch test files. Call it when the review is done.
func (tb *Toolbox) Cleanup() {
	for _, p := range tb.scratch {
		os.Remove(p)
	}
	tb.scratch = nil
}

// command runs git or go inside the repository with a timeout and a clean environment.
func (tb *Toolbox) command(ctx context.Context, name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, tb.timeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Dir = tb.root
	cmd.Env = cleanEnv()
	cmd.WaitDelay = 5 * time.Second // don't hang on child processes that keep the output open

	started := time.Now()
	out, err := cmd.CombinedOutput()
	took := time.Since(started).Round(time.Second)

	switch {
	case ctx.Err() == context.DeadlineExceeded:
		return fmt.Sprintf("timed out after %s\n%s", tb.timeout, out), nil
	case err != nil && cmd.ProcessState == nil:
		return "", err // the command couldn't start at all
	}
	// A non-zero exit (failing tests, vet findings) is a normal result for the model to read.
	return fmt.Sprintf("$ %s %s\nexit code %d after %s\n%s", name, strings.Join(args, " "), cmd.ProcessState.ExitCode(), took, out), nil
}

// cleanEnv is the environment for git and go: enough to build and test, and no secrets.
func cleanEnv() []string {
	env := []string{"GOPROXY=off", "GOFLAGS=-mod=mod", "GOTOOLCHAIN=local", "GIT_TERMINAL_PROMPT=0"}
	for _, name := range []string{"PATH", "HOME", "TMPDIR", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE"} {
		if value := os.Getenv(name); value != "" {
			env = append(env, name+"="+value)
		}
	}
	return env
}

// packagePatterns splits "./... ./cmd" into patterns, refusing flags and module@version downloads.
func packagePatterns(s string) ([]string, error) {
	patterns := strings.Fields(s)
	if len(patterns) == 0 {
		return []string{"./..."}, nil
	}
	for _, p := range patterns {
		if strings.HasPrefix(p, "-") || strings.Contains(p, "@") {
			return nil, fmt.Errorf("%q is not a package pattern like ./... or ./internal/foo", p)
		}
		if p != "." && !strings.HasPrefix(p, "./") || slices.Contains(strings.Split(p, "/"), "..") {
			return nil, fmt.Errorf("%q: package patterns must start with ./ and stay inside the repository", p)
		}
	}
	return patterns, nil
}

// truncate keeps the start and end of long output (errors are usually at the end) and says what was cut.
func truncate(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	head := limit / 2
	tail := limit - head
	return s[:head] + fmt.Sprintf("\n\n[... %d bytes cut: output was longer than %d bytes ...]\n\n", len(s)-limit, limit) + s[len(s)-tail:]
}
