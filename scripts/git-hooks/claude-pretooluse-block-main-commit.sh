#!/usr/bin/env bash
# Claude Code PreToolUse hook for the Bash tool.
# Block `git commit` (and `git commit -m`, `git commit -am`, etc.) when on main/master.
# Receives the tool input JSON on stdin.
#
# Exit 0 → allow the Bash invocation.
# Exit 2 → block, print stderr message to Claude (per Claude Code hook protocol).

set -euo pipefail

# Read tool input JSON from stdin.
INPUT=$(cat)

# Extract the command field. jq is the right tool but may not be installed; fall
# back to grep on the raw JSON. The command field looks like:
#   "command": "git commit -m \"...\""
COMMAND=""
if command -v jq >/dev/null 2>&1; then
  COMMAND=$(printf "%s" "$INPUT" | jq -r '.tool_input.command // empty')
else
  # Crude extraction; good enough for the command we're guarding.
  COMMAND=$(printf "%s" "$INPUT" | sed -nE 's/.*"command"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' | head -1)
fi

# Only act on `git commit` invocations.
if [[ "$COMMAND" =~ (^|[[:space:];&|])git[[:space:]]+commit($|[[:space:]]) ]]; then
  # Resolve the *actual* working directory for the commit. The hook runs
  # before the bash command executes, so $(pwd) gives the SHELL's cwd —
  # which is wrong when the command is `cd worktree-dir && git commit ...`
  # or uses `git -C worktree-dir commit ...`. Detect both patterns.
  RESOLVED_DIR=""

  # Pattern 1: `cd <dir> && git commit ...` (anywhere in the command).
  if [[ "$COMMAND" =~ (^|[[:space:];&|])cd[[:space:]]+(\"?)([^[:space:]\"\']+)\"?[[:space:]]*\&\& ]]; then
    RESOLVED_DIR="${BASH_REMATCH[3]}"
  fi
  # Pattern 2: `git -C <dir> commit ...`.
  if [[ -z "$RESOLVED_DIR" && "$COMMAND" =~ git[[:space:]]+-C[[:space:]]+(\"?)([^[:space:]\"\']+)\"?[[:space:]]+commit ]]; then
    RESOLVED_DIR="${BASH_REMATCH[2]}"
  fi
  # Default: shell's pwd at hook time.
  if [ -z "$RESOLVED_DIR" ]; then
    RESOLVED_DIR="$(pwd)"
  fi

  BRANCH=$(git -C "$RESOLVED_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
  if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
    cat >&2 <<EOF
❌  Refusing 'git commit' on protected branch '$BRANCH'.

Workflow rule (saved as project memory): all commits land via feature branch → PR → code review → auto-merge.

Recover:
  git checkout -b <feature-branch-name>
  <re-run the git commit>

Or override with:
  git commit --no-verify
EOF
    exit 2
  fi
fi

exit 0
