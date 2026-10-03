.PHONY: help bedrock-role bedrock-role-arn bedrock-smoke bedrock-smoke-all bedrock-models

PROFILE ?= demos-admin
REGION ?= us-west-2
STACK := pr-review-agent-bedrock
MODEL ?= qwen.qwen3-coder-30b-a3b-v1:0
# The models compared by youtube-outliers' compare-models workflow (keep the two lists in sync).
MODELS ?= qwen.qwen3-coder-480b-a35b-v1:0 deepseek.v3.2 global.anthropic.claude-sonnet-5-5 us.anthropic.claude-haiku-4-5-20251001-v1:0
# GitHub's OIDC subject for a repo; repos with immutable subjects use OWNER@ID/REPO@ID (see
# gh api repos/OWNER/REPO/actions/oidc/customization/sub), so allow both forms.
SUBJECTS ?= repo:marianina8/youtube-outliers:*,repo:marianina8@8147854/youtube-outliers@1382389609:*

help:
	@echo "make bedrock-smoke      one tiny Bedrock call with your own profile (checks model access)"
	@echo "make bedrock-smoke-all  the same tiny call against every model in MODELS"
	@echo "make bedrock-models     list the Qwen Coder / DeepSeek / Claude model IDs available in $(REGION)"
	@echo "make bedrock-role       deploy the GitHub OIDC role that can call only the listed model families"
	@echo "make bedrock-role-arn   print the role ARN to set as BEDROCK_REVIEW_ROLE_ARN"
	@echo "Overrides: PROFILE=$(PROFILE) REGION=$(REGION) SUBJECTS='repo:OWNER/REPO:*,...'"

# Creates the GitHub OIDC provider too, unless another stack or tool already made one.
# If this stack created it, keep it: passing false on an update would make CloudFormation delete it.
bedrock-role:
	@if aws cloudformation describe-stack-resource --stack-name $(STACK) --logical-resource-id GitHubOIDCProvider \
			--profile $(PROFILE) --region $(REGION) > /dev/null 2>&1; then \
		create=true; echo "GitHub OIDC provider is managed by this stack; keeping it"; \
	elif aws iam list-open-id-connect-providers --profile $(PROFILE) --output text | grep -q token.actions.githubusercontent.com; then \
		create=false; echo "GitHub OIDC provider already exists outside this stack; reusing it"; \
	else create=true; fi; \
	aws cloudformation deploy --template-file infra/github-bedrock-role.yaml --stack-name $(STACK) \
		--capabilities CAPABILITY_NAMED_IAM --profile $(PROFILE) --region $(REGION) \
		--tags app=pr-review-agent data=demo \
		--parameter-overrides "AllowedSubjects=$(SUBJECTS)" CreateOIDCProvider=$$create
	@$(MAKE) --no-print-directory bedrock-role-arn

bedrock-role-arn:
	@aws cloudformation describe-stacks --stack-name $(STACK) --profile $(PROFILE) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text

bedrock-smoke:
	aws bedrock-runtime converse --model-id $(MODEL) --profile $(PROFILE) --region $(REGION) \
		--messages '[{"role":"user","content":[{"text":"Reply with the word ok."}]}]' \
		--query 'output.message.content[0].text' --output text

bedrock-smoke-all:
	@for m in $(MODELS); do \
		printf '%-50s ' "$$m"; \
		aws bedrock-runtime converse --model-id "$$m" --profile $(PROFILE) --region $(REGION) \
			--messages '[{"role":"user","content":[{"text":"Reply with the word ok."}]}]' \
			--inference-config '{"maxTokens":10}' \
			--query 'output.message.content[0].text' --output text 2>&1 | head -n 1; \
	done

bedrock-models:
	@echo "== foundation models"; \
	aws bedrock list-foundation-models --profile $(PROFILE) --region $(REGION) --output text \
		--query "modelSummaries[?contains(modelId,'qwen3-coder') || contains(modelId,'deepseek') || contains(modelId,'claude-sonnet') || contains(modelId,'claude-haiku')].[modelId]"
	@echo "== inference profiles (Claude is called through these)"; \
	aws bedrock list-inference-profiles --profile $(PROFILE) --region $(REGION) --output text \
		--query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude-sonnet') || contains(inferenceProfileId,'claude-haiku')].[inferenceProfileId]"
