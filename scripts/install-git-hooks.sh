#!/usr/bin/env bash
# Install repo-tracked git hooks into .git/hooks/.
# .git/hooks/ is not tracked by git, so each clone needs to run this once.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HOOKS_SRC="$SCRIPT_DIR/git-hooks"
HOOKS_DST="$REPO_ROOT/.git/hooks"

if [ ! -d "$HOOKS_DST" ]; then
  echo "❌  Not a git checkout: $REPO_ROOT/.git/hooks missing"
  exit 1
fi

for hook in "$HOOKS_SRC"/pre-commit; do
  if [ ! -f "$hook" ]; then
    continue
  fi
  name=$(basename "$hook")
  cp "$hook" "$HOOKS_DST/$name"
  chmod +x "$HOOKS_DST/$name"
  echo "✓ installed $name"
done

echo ""
echo "Done. Hooks active for this clone."
echo ""
echo "Workflow protected: 'git commit' on main/master is blocked at the git layer."
echo "Claude Code sessions are also blocked via .claude/settings.json (PreToolUse hook)."
