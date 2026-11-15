#!/usr/bin/env bash
# PreToolUse guard for Bash. Exit 2 blocks the command and shows stderr to Claude.
#  - git push: never a force-push, never the local backup branch.
#  - git commit: no AI co-author trailers; lint, tests and the mock holdout must pass.
cmd=$(jq -r '.tool_input.command // ""')
cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 1

if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push([[:space:]]|$)'; then
  push=$(printf '%s' "$cmd" | grep -Eo 'git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push[^;&|]*')
  if printf '%s' "$push" | grep -q 'backup/trusted-core-before-rewrite'; then
    echo "Blocked: backup/trusted-core-before-rewrite is local only and is never pushed." >&2; exit 2
  fi
  if printf '%s' "$push" | grep -Eq '(^|[[:space:]])(--force|--force-with-lease|--force-if-includes|-f|--mirror)([=[:space:]]|$)|[[:space:]]\+[^[:space:]]'; then
    echo "Blocked: force-pushes are never allowed on this project." >&2; exit 2
  fi
fi

if printf '%s' "$cmd" | grep -Eq '(^|[;&|[:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit([[:space:]]|$)'; then
  if printf '%s' "$cmd" | grep -Eiq 'co-authored-by|generated with claude code'; then
    echo "Blocked: commit messages carry no AI co-author or 'Generated with' lines." >&2; exit 2
  fi
  export PYTHONPATH=src
  out=$( { .venv/bin/ruff check . && .venv/bin/pytest -q -x \
           && .venv/bin/python -m secure_query.evals.run --split holdout --provider mock; } 2>&1 )
  if [ $? -ne 0 ]; then
    { echo "Blocked: pre-commit checks failed (ruff, pytest, mock holdout). Last lines:"; printf '%s\n' "$out" | tail -25; } >&2
    exit 2
  fi
fi
exit 0
