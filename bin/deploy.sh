#!/usr/bin/env bash
# bin/deploy.sh — atomic Railway deploy with BUILD_COMMIT injection.
#
# Wraps `railway up` so /health reports the actual git SHA that built
# the running container — closes Plan #42 Track Z verification for the
# `railway up`-driven deploy path. (Railway's auto-injected
# RAILWAY_GIT_COMMIT_SHA only populates on GitHub-triggered builds,
# which we don't use; auto-deploy is OFF per CLAUDE.md.)
#
# What this does, in order:
#   1. Refuse to run if the working tree is dirty (committed code only).
#   2. Refuse to run if HEAD is not on main (no accidental feature-branch
#      deploys). Override with --allow-non-main if you're a human who
#      knows what you're doing.
#   3. Resolve the current git SHA (HEAD on main).
#   4. Set BUILD_COMMIT as a Railway service variable WITHOUT triggering
#      an early deploy.
#   5. Run `railway up --service "GTM Health Agent" --detach` so the
#      build picks up the just-set BUILD_COMMIT.
#   6. Print the build-log URL so the operator can watch it settle.
#
# Verification (Plan #42 Z2): once /ready returns 200, hit /health and
# confirm build_commit matches `git rev-parse main`. The wrapper
# doesn't poll for you — kept lightweight so it composes cleanly with
# the operator's existing monitoring (Slack admin DM on smoke probe
# outcome, manual Railway dashboard, etc.).
#
# Usage:
#   bin/deploy.sh                       # standard deploy from main
#   bin/deploy.sh --allow-non-main      # deploy a feature branch
#   bin/deploy.sh --dry-run             # print steps, do nothing
set -euo pipefail

# Railway service name. Override per fork: export RAILWAY_SERVICE="Your Service".
SERVICE="${RAILWAY_SERVICE:-gtm-health-agent}"
ALLOW_NON_MAIN=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --allow-non-main) ALLOW_NON_MAIN=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h)
      sed -n '1,32p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown arg: $arg (use --help)" >&2
      exit 2
      ;;
  esac
done

# Refuse on dirty tree.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: working tree has uncommitted changes." >&2
  echo "       commit or stash before deploying — the goal is to deploy" >&2
  echo "       a SHA that matches what's in the repo." >&2
  exit 1
fi

# Refuse non-main without override.
BRANCH=$(git branch --show-current 2>/dev/null || echo "")
if [ "$BRANCH" != "main" ] && [ "$ALLOW_NON_MAIN" -eq 0 ]; then
  echo "ERROR: HEAD is on branch '$BRANCH', not main." >&2
  echo "       pass --allow-non-main to deploy a feature branch." >&2
  exit 1
fi

SHA=$(git rev-parse HEAD)
SHORT="${SHA:0:8}"

echo "Deploying:"
echo "  Service:  $SERVICE"
echo "  Branch:   $BRANCH"
echo "  Commit:   $SHORT ($SHA)"
echo ""

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY RUN — would run:"
  echo "  railway variables --set BUILD_COMMIT=$SHA --service \"$SERVICE\" --skip-deploys"
  echo "  railway up --service \"$SERVICE\" --detach"
  exit 0
fi

echo "Setting BUILD_COMMIT=$SHORT on Railway (no deploy trigger)..."
railway variables --set "BUILD_COMMIT=$SHA" --service "$SERVICE" --skip-deploys

echo ""
echo "Uploading + deploying..."
railway up --service "$SERVICE" --detach

echo ""
echo "Deploy triggered. Verify once /ready=200:"
echo "  curl -s https://your-app.up.railway.app/health | jq .build_commit"
echo "  expected: \"$SHA\""
