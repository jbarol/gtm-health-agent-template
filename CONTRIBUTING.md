# Contributing

Builder-to-builder notes for working on this codebase. If you forked the template to run your
own portco, the same conventions apply: tests, type-check, lint, and the prompt-deploy gate are
all part of the loop.

This repo has no `Makefile` and no `pyproject.toml`. The commands below are the whole toolchain.

## 1. Dev setup

You need **Python 3.12** (pinned in `pyrightconfig.json`; the Dockerfile builds on it too).

```bash
# from the repo root
python --version                       # expect 3.12.x
pip install -r orchestrator/requirements.txt
```

That one requirements file covers the orchestrator, the agent provisioning scripts, and the
`bin/` utilities. There is no separate test-deps file. Copy `.env.example` to `.env` and fill in
your IDs before running anything that touches the Anthropic, Slack, or Salesforce APIs. The
Postgres schema bootstraps itself at boot (`db_adapter.ensure_schema` plus the `migrations/`
runner), so you do not run `psql` by hand.

## 2. Tests

Tests use the **`*_test.py`** suffix, not pytest's default `test_*.py`. Pytest will not discover
them unless you pass the pattern explicitly. `orchestrator/conftest.py` loads `.env` and stubs
`slack_bolt.App` at collection time, so importing modules does not open a Slack connection.

```bash
# full suite (orchestrator + agents + bin)
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator agents bin

# single file
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator/session_runner_test.py

# single test
pytest -p no:cacheprovider -o python_files='*_test.py' orchestrator/session_runner_test.py::test_something
```

**`*_smoke_test.py` files hit live APIs** using the loaded `.env` (Anthropic, Slack, Salesforce).
Run them deliberately when you want to verify a real integration. Do **not** wire them into
routine CI or run them as part of the command above. Use `.env.smoke.example` as the template for
the credentials a smoke run needs.

## 3. Type-check and lint

```bash
pyright                                    # config in pyrightconfig.json, Python 3.12
ruff check orchestrator agents bin scripts
```

`pyright` reads `pyrightconfig.json` (includes `orchestrator`, `agents`, `bin`, `scripts`; treats
missing imports as errors). `ruff` has no `ruff.toml` and runs on defaults. Both should be clean
before you open a PR.

## 4. Branch and PR workflow

A pre-commit hook **blocks commits on the default branch**. Branch first, every time:

```bash
git checkout -b feat/short-description
```

Use **conventional-commit** subject lines (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
`test:`). Keep the subject under ~72 characters and let the body explain the why.

Open a pull request when the branch is ready. The **codex review** workflow
(`.github/workflows/codex-review.yml`) runs on every PR and posts findings as a PR comment. It is
advisory (the job is `continue-on-error`), but it flags a workflow failure on `CRITICAL` findings
read its comment and resolve anything it raises before you merge.

## 5. Agent prompt changes

Agent system prompts are **deployed by `agents/update_prompts.py`**, which pushes the prompt text
to the Anthropic side for each agent ID. You do not edit prompts on the Anthropic console the
source of truth lives in `agents/update_prompts.py` (and `orchestrator/writing_agent.py` for the
Writing Agent prose prompt).

Deploys are automated by `.github/workflows/deploy-prompts.yml`, which runs `update_prompts.py`
whenever a merge to the default branch touches a prompt source file (`agents/setup_agents.py`,
`agents/update_prompts.py`, `orchestrator/writing_agent.py`, or `agents/update_subagent_tools.py`).

**The `prompt-author-verified` label gates the deploy.** The workflow looks up the merged PR that
owns the triggering commit and fails loud if that PR does not carry the `prompt-author-verified`
label. This is a tripwire for forgetfulness, not a security control there is no second reviewer it
forces you to re-read the prompt diff one more time before it reaches Anthropic. Add the label to
your PR before merging any prompt change.

**Rollback** if a deployed prompt regresses behavior:

```bash
python bin/rollback-agent.py <agent_short_name> --to-version <N>
# e.g.
python bin/rollback-agent.py coordinator --to-version 26
```

The agent IDs the workflow and the rollback script use come from env vars (`COORDINATOR_ID`,
`STATISTICIAN_ID`, and so on), set as repo secrets and in `.env`. Nothing is hardcoded. For a
whole-deploy rollback, `bin/rollback-deploy.py` walks the artifact bracket; see
`docs/runbook-prompt-rollback.md`.

## 6. Where things live

Context is split across nested `CLAUDE.md` files. Claude Code (and Codex/Cursor via the mirrored
`AGENTS.md`) auto-discover the one nearest the files you are editing:

| Path | Covers |
|---|---|
| `CLAUDE.md` (root) | What this is, running, testing, multi-portco, SOQL rules, SF schema expectations, deploy/break-glass |
| `agents/CLAUDE.md` | All agents plus out-of-roster, provisioning, prompt deploys, label gate, rollback, toolset versioning, conformance audits |
| `orchestrator/CLAUDE.md` | Session and custom-tool lifecycle, watchdog timing, `/health`, cost tracking, Watcher runtime, the optional knowledge base, env vars |
| `bin/CLAUDE.md` | `deploy.sh`, rollback wrappers, scrub-portco, audit scripts, backfill scripts |

`AGENTS.md` at the repo root is **auto-generated from the root `CLAUDE.md`** edit `CLAUDE.md`,
never `AGENTS.md`. The generator does not recurse into nested `CLAUDE.md` files, so anything that
must reach a cross-tool agent (SOQL constraints, SF custom-field expectations, `BUILD_COMMIT`
discipline) is kept in the root file on purpose.
