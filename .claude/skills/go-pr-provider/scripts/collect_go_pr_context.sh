#!/usr/bin/env bash
set -euo pipefail

requested_target="${1:-}"
review_mode="${2:-pr}"
base_ref="${3:-origin/main}"
head_ref="${4:-HEAD}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: not inside a git repository" >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

report_file="${TMPDIR:-/tmp}/go-pr-context-$(date +%Y%m%d%H%M%S).md"
vet_out="${TMPDIR:-/tmp}/go-vet-$$.log"
test_out="${TMPDIR:-/tmp}/go-test-$$.log"
diff_file="${TMPDIR:-/tmp}/go-diff-$$.patch"

diff_source="working-tree"
if [[ "$review_mode" == "pr" ]]; then
  if git rev-parse --verify "$base_ref" >/dev/null 2>&1 && git rev-parse --verify "$head_ref" >/dev/null 2>&1; then
    git diff "$base_ref...$head_ref" >"$diff_file"
    diff_source="$base_ref...$head_ref"
  else
    git diff >"$diff_file"
  fi
else
  {
    echo "# code-review mode"
    echo "No PR diff requested."
    if [[ -n "$requested_target" ]]; then
      echo "Target provided: $requested_target"
    fi
  } >"$diff_file"
  diff_source="code-review"
fi

target_file=""
if [[ -n "$requested_target" ]]; then
  if [[ -f "$requested_target" ]]; then
    target_file="$requested_target"
  elif [[ -d "$requested_target" ]]; then
    target_file="$(find "$requested_target" -type f -name '*.go' | head -n 1 || true)"
  fi
fi

if [[ -z "$target_file" ]]; then
  target_file="$(git diff --name-only -- '*.go' | head -n 1 || true)"
fi

if [[ -z "$target_file" && "$review_mode" == "code" ]]; then
  target_file="$(find . -type f -name '*.go' | head -n 1 || true)"
  target_file="${target_file#./}"
fi

if [[ -z "$target_file" && -f go.mod ]]; then
  target_file="go.mod"
fi

set +e
go vet ./... >"$vet_out" 2>&1
vet_status=$?
go test ./... >"$test_out" 2>&1
test_status=$?
set -e

{
  echo "# Diff Summary"
  echo
  echo "Source: $diff_source"
  echo
  echo '```diff'
  cat "$diff_file"
  echo '```'
  echo

  echo "# File Read"
  echo
  if [[ -n "$target_file" && -f "$target_file" ]]; then
    echo "Target: $target_file"
    echo
    echo '```'
    cat "$target_file"
    echo '```'
  else
    echo "Target: (none selected)"
  fi
  echo

  echo "# go vet"
  echo
  echo "Exit Code: $vet_status"
  echo
  echo '```'
  cat "$vet_out"
  echo '```'
  echo

  echo "# go test"
  echo
  echo "Exit Code: $test_status"
  echo
  echo '```'
  cat "$test_out"
  echo '```'
  echo

  echo "# Next Action"
  if [[ $vet_status -eq 0 && $test_status -eq 0 ]]; then
    echo "All checks passed. Proceed with PR review and merge readiness assessment."
  else
    echo "Investigate failing vet/test output and fix issues before merge."
  fi
} >"$report_file"

rm -f "$vet_out" "$test_out" "$diff_file"

echo "$report_file"
