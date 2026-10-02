---
name: go-pr-provider
description: "Route Go PR validation by provider flag using one shared check pipeline, then summarize with Claude or local Ollama. Runs git diff, file read, go vet, and go test."
argument-hint: "provider=claude|ollama review_mode=pr|code target=path/to/file-or-dir base_ref=origin/main head_ref=HEAD ollama_model=qwen2.5-coder:7b"
user-invocable: true
---

# Go PR Provider Router

## When to Use
- You want one command-like workflow for Go PR checks.
- You want to review direct commits or any code even without an open PR.
- You want to choose review provider with a flag:
- `provider=claude` to summarize with Claude in chat.
- `provider=ollama` to summarize with local `ollama`.

## Arguments
- `provider` (required): `claude` or `ollama`
- `review_mode` (optional):
- `pr` (default): produce diff from refs when available
- `code`: review target code even without a PR diff
- `target` (optional): file or directory to inspect
- `base_ref` (optional, `review_mode=pr`): default `origin/main`
- `head_ref` (optional, `review_mode=pr`): default `HEAD`
- `ollama_model` (optional, only for `provider=ollama`): default `qwen2.5-coder:7b`

## Procedure
1. Parse arguments.
2. Run [collect_go_pr_context.sh](./scripts/collect_go_pr_context.sh) for both providers to gather the exact same review context:
- diff context:
- in `pr` mode, use `git diff <base_ref>...<head_ref>` when refs exist
- otherwise fall back to working tree `git diff`
- target code content
- `go vet ./...`
- `go test ./...`
3. Route by provider:
- If `provider=claude`, read the generated report and return the final review directly in chat.
- If `provider=ollama`, run [review_with_ollama.sh](./scripts/review_with_ollama.sh) with selected model and generated report.
4. Return results using this section order:
- `Diff Summary`
- `File Read`
- `go vet`
- `go test`
- `Next Action`

## Notes
- The check execution code path is identical for both providers because both must run [collect_go_pr_context.sh](./scripts/collect_go_pr_context.sh).
- This supports PR review and non-PR code review using the same collector.
- For `provider=ollama`, this skill requires local `ollama` CLI and a pulled model.
- Recommended first run:
- `ollama pull qwen2.5-coder:7b`

## Examples
- PR-style review from branch refs:
- `provider=claude review_mode=pr base_ref=origin/main head_ref=HEAD`
- Review direct-commit changes (working tree fallback):
- `provider=claude review_mode=pr`
- Review any code file or folder without PR context:
- `provider=ollama review_mode=code target=internal/service`
