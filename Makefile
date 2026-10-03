.PHONY: help bedrock-role bedrock-role-arn bedrock-smoke

PROFILE ?= demos-admin
REGION ?= us-west-2
STACK := pr-review-agent-bedrock
MODEL ?= qwen.qwen3-coder-30b-a3b-v1:0
SUBJECTS ?= repo:marianina8/youtube-outliers:*

help:
	@echo "make bedrock-smoke      one tiny Bedrock call with your own profile (checks model access)"
	@echo "make bedrock-role       deploy the GitHub OIDC role that can call only $(MODEL)"
	@echo "make bedrock-role-arn   print the role ARN to set as BEDROCK_REVIEW_ROLE_ARN"
	@echo "Overrides: PROFILE=$(PROFILE) REGION=$(REGION) SUBJECTS='repo:OWNER/REPO:*,...'"

# Creates the GitHub OIDC provider too, unless the account already has one.
bedrock-role:
	@if aws iam list-open-id-connect-providers --profile $(PROFILE) --output text | grep -q token.actions.githubusercontent.com; then \
		create=false; echo "GitHub OIDC provider already exists; reusing it"; else create=true; fi; \
	aws cloudformation deploy --template-file infra/github-bedrock-role.yaml --stack-name $(STACK) \
		--capabilities CAPABILITY_NAMED_IAM --profile $(PROFILE) --region $(REGION) \
		--tags app=pr-review-agent data=demo \
		--parameter-overrides "AllowedSubjects=$(SUBJECTS)" "ModelId=$(MODEL)" CreateOIDCProvider=$$create
	@$(MAKE) --no-print-directory bedrock-role-arn

bedrock-role-arn:
	@aws cloudformation describe-stacks --stack-name $(STACK) --profile $(PROFILE) --region $(REGION) \
		--query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text

bedrock-smoke:
	aws bedrock-runtime converse --model-id $(MODEL) --profile $(PROFILE) --region $(REGION) \
		--messages '[{"role":"user","content":[{"text":"Reply with the word ok."}]}]' \
		--query 'output.message.content[0].text' --output text
