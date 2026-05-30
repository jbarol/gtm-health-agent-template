---
adr_id: 0001
title: Schema-driven `post_report` tool over JSON-as-text in `send_slack_notification`
status: accepted
date: 2026-05-11
context: GTM Health Agent v1 structured-output shipped 2026-05-11
related: ADR-002 (future — native structured outputs migration, Plan #34)
implements: CEO #5 finding from /autoplan dual-voice review
---

# ADR-0001 — Schema-driven `post_report` tool over JSON-as-text in `send_slack_notification`

## Status

Accepted. Implemented 2026-05-11 via commits 7a64d61 → a388b63.

## Context

In May 2026 we needed standardized agent output to Slack. Agents had been emitting free-form prose through the existing `send_slack_notification` custom tool, leading to inconsistent length, missing fields, and no way to validate before sending. The /autoplan CEO review explored options and we shipped a new `post_report` tool with Pydantic schemas + a renderer (`orchestrator/response_renderer.py`).

After implementation, the /autoplan dual-voice review (Codex + Claude subagent) surfaced a fourth option — **Approach D** — that the original options analysis missed. This ADR documents it as a historical record so future similar decisions consider it.

## The three approaches that WERE considered

- **Approach A** — Tighter prompts + post-hoc length validator. Lightweight but doesn't enforce structure; agents drift over time.
- **Approach B** — Schema-driven outputs via new `post_report` tool. (**SELECTED**.)
- **Approach C** — Layered progressive disclosure via Slack reactions / commands. Right idea for v2, wrong scope for v1.

## The fourth approach that WAS NOT considered (Approach D)

**Approach D — JSON-as-text inside existing `send_slack_notification`.**

The idea: keep the existing `send_slack_notification` tool. Instruct agents (via prompt) to emit a JSON string in the `detail` parameter whenever the post is a final deliverable. The orchestrator detects JSON, validates against the Pydantic schemas, renders, and posts. Agents that don't emit valid JSON fall back to the free-form path.

### What Approach D would have saved

1. **No new tool definition.** `send_slack_notification` already exists on every agent that posts to Slack. No `agents/add_post_report_tool.py` one-shot migration needed.
2. **No `setup_agents.py` redeploy.** Adding a tool requires editing setup_agents.py + running add_post_report_tool.py. JSON-in-text path needs only prompt updates.
3. **No `update_prompts.py` rewrite of every agent's `<output_format>` block.** Just adding a "for final reports emit a JSON string in `detail`" instruction.
4. **Simpler agent mental model.** "Use the same tool for everything" vs. "use Tool A for progress, Tool B for final reports."

### What Approach D would have cost

1. **Tool description ambiguity.** `send_slack_notification` would have two roles (progress chatter + final report). The split-routing pathology (CEO #10) becomes worse, not better.
2. **No tool-call enforcement.** The Managed Agents API doesn't validate `detail` parameter contents. A malformed JSON string would slip past the API into our orchestrator's JSON parse, costing a session round-trip per failure.
3. **Migration to native structured outputs (Plan #34) is harder.** The native API replaces a tool definition with a `response_format` field. A dedicated tool is a cleaner thing to replace than "this one string field but only when it's JSON."
4. **No prompt-side schema drift detection.** Our chosen approach (Plan #32) puts the JSON Schema in BOTH the tool input_schema AND the prompt. Approach D could only put it in the prompt, so drift is harder to catch.

## Why we still went with Approach B

The shipped approach (`post_report` tool + Pydantic + renderer) earned its complexity:
- Tool-level enforcement: the Managed Agents API can reject unknown response_type values before they reach our orchestrator (subject to Plan #32 wiring).
- Clean discriminator between "progress chatter" and "final report" — `send_slack_notification` stays content-free; `post_report` is the only structured path.
- Cleanest migration to native structured outputs (Plan #34) — the tool definition simply goes away.
- The "no JSON-in-text" rule is easier to teach the model than "JSON when final, prose when progress."

## Why we document this anyway

1. **Future architectural decisions in this codebase should consider Approach D-style options.** Any time we're tempted to add a new custom tool, ask "could the existing tool carry this with a discriminator?"
2. **If `post_report` adoption shows persistent split-routing drift** (Eng E10 / CEO #10), Approach D becomes a viable v2 refactor: collapse the two tools into one with a `kind: "progress" | "report"` field.
3. **If Anthropic's native structured outputs ship with a constrained shape** (e.g., schema applies to the WHOLE assistant message, not per-tool), Approach D's "one tool, two modes" becomes the closer fit.

## Consequences

- Plan #34 (native structured outputs migration) is designed assuming Approach B. If Approach D is ever revisited, that plan needs revision.
- CEO #10 (re-evaluate two-tools vs single-tool) is the canary: if split-routing drift exceeds ~5% after canary data, treat that as a signal to consider an Approach D-style collapse.

## References

- Original autoplan plan: `~/.claude/plans/next-office-hours-this-agent-serialized-stroustrup.md` lines 526-540
- Implementing commits: 7a64d61 (post_report tool), 511fed0 (ship-blocker fixes), 17d406b (cache math fix), a388b63 (Phase 3 prompts)
- Plan #34: `docs/plans/34-native-structured-outputs-migration.md`
- Plan #32: `docs/plans/32-json-schema-delivery-dx-2.md`
