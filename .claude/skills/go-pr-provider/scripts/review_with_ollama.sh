#!/usr/bin/env bash
set -euo pipefail

model="${1:-qwen2.5-coder:7b}"
report_file="${2:-}"

if [[ -z "$report_file" || ! -f "$report_file" ]]; then
  echo "Usage: $0 <ollama_model> <report_file>" >&2
  exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama command is not found" >&2
  exit 1
fi

report_content="$(cat "$report_file")"

prompt="You are reviewing a Go code review context report (PR mode or direct code mode).\n\nReturn your answer with these exact section headers:\n1. Diff Summary\n2. File Read\n3. go vet\n4. go test\n5. Next Action\n\nFor each section, summarize risks, failures, and concrete fixes.\n\nReport:\n\n$report_content"

ollama run "$model" "$prompt"
