FROM python:3.12-slim

# Build args injected by the build system. Required for the /health endpoint
# (B10, Z2): the live container reports the commit it was built from so the
# deploy verification step can confirm Railway is actually running main HEAD
# instead of a stale binary (see 2026-05-11 stale-binary incident).
#
# Resolution order (first non-empty wins, BuildKit ARG-from-ARG default):
#   1. ``BUILD_COMMIT`` — explicit, takes precedence. Local / CI workflows
#      pass directly: ``--build-arg BUILD_COMMIT=$(git rev-parse HEAD)``
#      or ``--build-arg BUILD_COMMIT=${{ github.sha }}``.
#   2. ``RAILWAY_GIT_COMMIT_SHA`` — Railway-provided variable, auto-injected
#      by the builder at build time per
#      https://docs.railway.com/variables#railway-provided-variables. No
#      railway.toml plumbing required. Closes Plan #42 Track Z by giving
#      ``/health`` the real built SHA instead of stale "unknown".
#   3. ``unknown`` — loud-fail signal that neither source is wiring through.
ARG RAILWAY_GIT_COMMIT_SHA=unknown
ARG BUILD_COMMIT=$RAILWAY_GIT_COMMIT_SHA
ENV BUILD_COMMIT=$BUILD_COMMIT

# Plan #42 PR2 — Pre-deploy smoke probe kill switch. When true (default), the
# orchestrator runs ``smoke_probe.run_smoke_probe()`` before binding the
# ``/ready`` health route and refuses to start Socket Mode if any check
# fails. Flip to false on Railway (Variables → Build) to bypass the gate
# during an incident — admin DM warns the deploy was not validated.
ARG SMOKE_PROBE_ENABLED=true
ENV SMOKE_PROBE_ENABLED=$SMOKE_PROBE_ENABLED

WORKDIR /app

# Install system deps. ``git`` is required by the weekly prompt-patches
# promoter cron (orchestrator.promote_prompt_patches → subprocess git diff +
# git apply). Observed failure 2026-05-16 Sat 09:00 PT: cron crashed with
# ``FileNotFoundError: [Errno 2] No such file or directory: 'git'`` because
# python:3.12-slim ships without git. The promoter is best-effort, but
# without git no draft PRs ever fire and prompt-patches accumulate
# uncommitted in the health memory store.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator/ ./orchestrator/
COPY agents/ ./agents/
COPY rubrics/ ./rubrics/
COPY skills/ ./skills/
COPY memory/ ./memory/
# Copy the portco config. The example always exists in the build context; a
# real portco_config.json (gitignored) is copied too when a forker has created
# one. The cp -n fallback guarantees portco_config.json exists in the image
# (real if provided, else the example), so `docker build` never fails on a
# fresh fork. A forker overrides it by editing portco_config.json before build
# or mounting it at runtime.
COPY portco_config*.json ./
RUN cp -n portco_config.example.json portco_config.json 2>/dev/null || true

WORKDIR /app/orchestrator

CMD ["python", "main.py"]
