# pr-review-agent

Go code review in three forms that share the same checks (`go vet`, `go test`, the diff and the code):

- **Claude Code skill** — `.claude/skills/go-pr-provider`, run as `/go-pr-provider provider=claude|ollama ...`.
- **Agent definition** — `.github/agents/go-pr-check.agent.md`.
- **GitHub Actions** — a reusable workflow that reviews with either an Ollama model on the GitHub runner or a model on Amazon Bedrock, so nothing runs on your machine.

## GitHub Actions: Ollama review

Add this file to any Go repo as `.github/workflows/ollama-review.yml`:

```yaml
name: Ollama review
on:
  pull_request:
    paths: ["**.go", "go.mod", "go.sum"]
  workflow_dispatch:
    inputs:
      target: { description: "File or folder to review", default: "." }
permissions:
  contents: read
  pull-requests: write
  id-token: write          # only needed for provider: bedrock
jobs:
  review:
    uses: marianina8/pr-review-agent/.github/workflows/ollama-review.yml@main
    with:
      mode: ${{ github.event_name == 'pull_request' && 'pr' || 'code' }}
      target: ${{ inputs.target || '.' }}
      provider: ollama     # or bedrock
      aws_role_arn: ${{ vars.BEDROCK_REVIEW_ROLE_ARN }}
```

- **On a pull request** it reviews only the changed Go files (their diff plus the full file) and posts one comment on the PR, updated on each push.
- **From the Actions tab** ("Run workflow") it reviews every Go file under `target`. The review appears on the run's summary page.
- Both upload the full report and the raw inputs as the `ollama-review` artifact.

How it works:

1. `ci/collect.py` runs `go vet` and `go test`, then splits the code into chunks of whole packages that fit the model's context window, with numbered lines so findings cite `path:line`.
2. `ci/review.py` calls Ollama's API once per chunk with an explicit `num_ctx` (the CLI default of ~4k tokens silently cuts off long prompts), then once more for the Next Action list. The `go vet` and `go test` sections come straight from the tool output, not the model.
3. The report warns when a chunk may not have fit and lists tokens and seconds per call.

### Bedrock instead of Ollama

`provider: bedrock` sends the same chunks and the same instructions to Amazon Bedrock (default model `qwen.qwen3-coder-30b-a3b-v1:0`, an open-weight Qwen model hosted by AWS). GitHub signs in to AWS with OIDC, so no AWS keys are stored anywhere.

One-time setup (needs the AWS CLI and the `demos-admin` profile; override with `PROFILE=` / `REGION=`):

```bash
make bedrock-smoke                 # one tiny call with your own login: checks the model is usable
make bedrock-role                  # IAM role GitHub can assume; it may only invoke that one model
gh variable set BEDROCK_REVIEW_ROLE_ARN -R OWNER/REPO --body "$(make -s bedrock-role-arn)"
```

To allow more repos: `make bedrock-role SUBJECTS='repo:OWNER/a:*,repo:OWNER/b:*'`. The calling workflow must grant `id-token: write` (the example above does).

Inputs (all optional): `mode` (`pr`|`code`), `target`, `provider` (`ollama`|`bedrock`), `model`, `aws_role_arn`, `aws_region` (us-west-2), `num_ctx` (16384), `chunk_chars` (30000), `comment` (true), `agent_ref` (main).

Expect roughly 5–10 minutes per chunk on the standard 4-CPU runner; the model download is cached between runs. The workflow uses `pull_request`, never `pull_request_target`, so code from forks runs without write access and gets no PR comment.

Run the same scripts locally (needs Ollama running):

```bash
python3 path/to/pr-review-agent/ci/collect.py --mode code
python3 path/to/pr-review-agent/ci/review.py --out .go-pr-review/ci/review.md
```
