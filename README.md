# pr-review-agent

A Go code review agent, written in Go. It gives a model a few tools (read files, grep, `git diff`, `go vet`, `go test`, and throwaway scratch tests), lets it investigate the repository, and prints a review.

The same binary runs on your machine and in GitHub Actions, with one of three model APIs behind the `--model` flag:

| `--model` | Talks to | Default `--model-name` | Needs |
|---|---|---|---|
| `claude` | Anthropic API | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| `bedrock` | Amazon Bedrock (Converse) | `qwen.qwen3-coder-480b-a35b-v1:0` | AWS credentials (profile, env, or the GitHub OIDC role) |
| `ollama` | Ollama (`OLLAMA_HOST`, default localhost) | `qwen2.5-coder:7b` | a model that supports tool calling |

## Run it

```bash
go install github.com/marianina8/pr-review-agent@latest

cd path/to/your/go/repo
pr-review-agent --model claude                                   # review everything under .
pr-review-agent --model ollama --target ./internal               # one folder, local model
pr-review-agent --model bedrock --model-name deepseek.v3.2       # any Bedrock model with tool use
pr-review-agent --model claude --mode pr --base origin/main      # only what this branch changes
```

The review goes to stdout and the progress (each tool call and the start of its result) goes to stderr, so `> review.md` keeps just the review. `--stats stats.json` also writes steps, tool calls, tokens and time.

Other flags: `--max-steps` (20), `--max-tokens` (4096 per call), `--max-output` (20000 bytes per tool result), `--tool-timeout` (2m), `--region` (bedrock), `--ollama-context` (32768; Ollama's own default is small and silently drops the start of long conversations).

## How it's built

| File | What it does |
|---|---|
| `main.go` | flags, picks the model, writes the first message |
| `agent.go` | the loop: ask the model, run the tools it asks for, send results back, stop when it answers |
| `tools.go` | the tools and their guardrails |
| `model.go` | the `Model` interface and the plain message types every model uses |
| `claude.go`, `bedrock.go`, `ollama.go` | translate those types to and from each API |
| `guidelines.md` | the reviewer's rules and output format, embedded in the binary |

To change what the reviewer looks for or how it writes, edit `guidelines.md` and rebuild.

## Guardrails

- Every tool is read-only except `write_scratch_test`, which may only create new `zz_review_*_test.go` files. They are deleted when the review ends.
- No shell. Each tool runs one fixed `git` or `go` command with its arguments passed as a list.
- Paths must stay inside the repository (`..`, absolute paths and symlinks out are refused).
- `go` and `git` run with a stripped-down environment (no API keys or cloud credentials) and `GOPROXY=off`, under a timeout.
- Large tool output is trimmed before the model sees it.
- Limit: a scratch test is real Go code. It runs as your user and can reach the network, so only review code you trust. The GitHub workflow checks out with `persist-credentials: false` so the token isn't on disk.

## GitHub Actions

`.github/workflows/code-review.yml` is a reusable workflow that builds this agent and runs it on the calling repo. Add this to any Go repo as `.github/workflows/code-review.yml`:

```yaml
name: AI code review
on:
  pull_request:
    paths: ["**.go", "go.mod", "go.sum"]
  workflow_dispatch:
permissions:
  contents: read
  pull-requests: write
  id-token: write          # only needed for provider: bedrock
jobs:
  review:
    uses: marianina8/pr-review-agent/.github/workflows/code-review.yml@main
    with:
      mode: ${{ github.event_name == 'pull_request' && 'pr' || 'code' }}
      provider: anthropic  # or bedrock, or ollama (runs on the GitHub runner; slow)
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

- **On a pull request** it reviews the changes and posts one comment, updated on each push.
- **From the Actions tab** it reviews everything under `target`; the review is on the run's summary page.
- Both upload the review, the agent's log and its stats as the `code-review` artifact.

Inputs (all optional): `mode` (`pr`|`code`), `target`, `provider` (`anthropic`|`bedrock`|`ollama`), `model`, `max_steps` (20), `max_output_tokens` (4096), `label`, `aws_role_arn`, `aws_region` (us-west-2), `num_ctx` (ollama, 32768), `comment` (true), `agent_ref` (main).

The workflow uses `pull_request`, never `pull_request_target`, so code from forks runs without secrets and gets no PR comment.

### Bedrock setup

GitHub signs in to AWS with OIDC, so no AWS keys are stored. One-time setup (needs the AWS CLI and the `demos-admin` profile; override with `PROFILE=` / `REGION=`):

```bash
make bedrock-smoke-all             # one tiny call per model with your own login: checks each is usable
make bedrock-role                  # IAM role GitHub can assume; it may only invoke the listed model families
gh variable set BEDROCK_REVIEW_ROLE_ARN -R OWNER/REPO --body "$(make -s bedrock-role-arn)"
```

The role allows Qwen3-Coder, DeepSeek, and Claude Sonnet, Haiku and Opus; change `ModelPatterns`/`ProfilePatterns` in `infra/github-bedrock-role.yaml` to allow others. To allow more repos: `make bedrock-role SUBJECTS='repo:OWNER/a:*,repo:OWNER/b:*'`.

### Comparing models

Pass a `label` per call to keep runs apart. youtube-outliers' `compare-models.yml` runs the agent with Qwen3-Coder 480B, DeepSeek V3.2, Claude Haiku 4.5 (Bedrock) and Claude Sonnet 5.5 (Anthropic API) in parallel on the same code.

## Other forms

- **Claude Code skill**: `.claude/skills/go-pr-provider`, run as `/go-pr-provider provider=claude|ollama ...` (shell scripts, not this agent).
- **Agent definition**: `.github/agents/go-pr-check.agent.md`.
