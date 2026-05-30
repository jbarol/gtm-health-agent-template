# Slack OAuth Scopes — Changelog

Audit trail for every OAuth scope granted to the GTM Health Agent Slack
app. The canonical declarative source is `manifest.yaml` at the repo
root. This file explains **why** each scope is on the bot and which
task added it. Any new scope MUST land with a row here and a paragraph
of justification — drive-by scope creep is the fastest way to lose
admin trust on a Slack workspace.

## How to read this file

Each row in the table maps one bot scope to the date it was granted,
the rationale, and the originating PR / plan task. The narrative
section below the table holds the longer-form justification for the
non-obvious scopes — anything outside the standard `chat:write` /
`commands` baseline.

## Scope table

| Scope                | Granted     | Why                                                                                            | Source                                                   |
| -------------------- | ----------- | ---------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `app_mentions:read`  | 2026-03-01  | Receive `@gtm-health-agent` mentions in channel — the primary ad-hoc question trigger.         | Initial bot install                                      |
| `chat:write`         | 2026-03-01  | Post messages (ack, findings, .xlsx attachments, charts) into portco channels and threads.    | Initial bot install                                      |
| `commands`           | 2026-03-01  | Register slash commands (`/cost`, `/feedback`, `/verbosity`, `/refresh-surface`).             | Initial bot install                                      |
| `im:write`           | 2026-03-01  | DM admins the daily cost digest, self-improvement release notes, and watch notices.           | Initial bot install                                      |
| `channels:read`      | 2026-03-01  | Resolve channel name → channel ID for portco-to-channel mapping in `portco_registry.py`.       | Initial bot install                                      |
| `groups:read`        | 2026-03-01  | Same resolver path for private channels (some portco channels are private).                    | Initial bot install                                      |
| `reactions:read`     | 2026-05-04  | Emoji-reaction feedback capture (Plan #30 D1) — :thumbsup:/:thumbsdown: on agent replies.      | PR #57 (`feat(feedback): emoji-reaction capture + schema`) |
| `reactions:write`    | 2026-05-13  | Lifecycle status reactions on the user's question (👁 received → ⏰ working → ✅ done / ❌ failed). | This PR — declarative parity with already-granted live token scope |
| `canvases:read`      | 2026-05-11  | Read existing channel canvas to detect adopt-existing branch in `upsert_slack_channel_canvas`.| PR #59 (`feat(slack): auto-upsert "How to Ask" canvas`)  |
| `canvases:write`     | 2026-05-11  | Create / edit channel canvases via `conversations.canvases.create` and `canvases.edit`.       | PR #59 (`feat(slack): auto-upsert "How to Ask" canvas`)  |
| `channels:manage`    | 2026-05-11  | Required by `conversations.canvases.create` — Slack treats canvas attach as a channel-mgmt op. | PR #59 (`feat(slack): auto-upsert "How to Ask" canvas`)  |
| `pins:write`         | 2026-05-11  | Reserved for the optional pinned-headline surface tier (Plan #33). Currently unused.           | This PR (Plan #33 F3) — pre-granted to avoid reinstall   |

## Narrative justification

### Canvas surface scopes (`canvases:read`, `canvases:write`, `channels:manage`)

PR #59 (commit `5fb3dd6`) introduced an auto-upserted "How to Ask"
channel canvas per portco. The implementation calls
`conversations.canvases.create` for new canvases and `canvases.edit`
for updates. Slack's API contract requires:

- `canvases:write` to create or edit any canvas body.
- `canvases:read` to read the existing canvas before deciding to
  create vs. edit (the script has an "adopt existing" branch keyed on
  whether the channel already has a canvas attached).
- `channels:manage` because `conversations.canvases.create` is
  modeled as a channel-management operation on Slack's side, not as a
  pure canvas operation. Without this scope the API returns
  `missing_scope` even with both canvas scopes granted.

These three scopes were granted on the **live** bot token at
PR #59 merge time but were **not** declarative in source. Plan #33
sub-task F3 closes that gap by adding them to `manifest.yaml`, so any
future reinstall of the app — or any new workspace install — picks
up the same grants without manual scope toggling in the Slack admin
UI.

### `pins:write` — pre-granted, currently unused

Plan #33 contemplates a future tier where each portco's headline
metric ("Pipeline coverage 2.1x, down from 2.4x last week") is
posted to the channel and **pinned** so it stays at the top of the
channel UI even if subsequent messages push the original out of
view. That tier is not yet implemented. The scope is granted now —
not at tier-ship time — to avoid a second admin reinstall round on
every workspace. Cost of pre-granting an unused scope: zero.
Cost of forcing a reinstall later: a coordination tax across every
portco admin.

### `reactions:write` — lifecycle status indicators

Live trace 2026-05-13 18:54 PT exposed a UX gap: between the rich ack
and the final findings, the user has no glanceable signal that the bot
is still working. For investigations that run 5+ minutes (the norm for
Coordinator-orchestrated multi-agent workflows), the only way to check
progress is to scroll the thread. Reactions on the user's original
message solve this without adding a poll-style "still running…" reply
chain that would clutter the channel.

The four reactions and their lifecycle transitions:

- 👁 `eye` — added on receipt, after dedup + mention-strip pass.
- ⏰ `alarm_clock` — added when the investigation worker actually
  starts the session (replaces `eye` on the same message).
- ✅ `white_check_mark` — added on successful `post_report` dispatch
  (replaces `alarm_clock`).
- ❌ `x` — added on catastrophic failure (replaces `alarm_clock`).

`reactions:write` was already granted on the live bot token via the
Slack admin UI before this PR landed. This entry brings the manifest
into declarative parity — same pattern as the canvas scopes (PR #59)
that landed live before being added to source. **No workspace
reinstall is required for this scope add.**

### `reactions:read` — feedback capture

Plan #30 D1 (PR #57) added :thumbsup: / :thumbsdown: emoji-reaction
capture on agent replies. Reactions feed into `feedback_events` and
roll up into `/feedback`. The scope only reads reaction events; it
does not write reactions.

### 2026-05-13 — Plan #44 admin slash commands (`/pin`, `/stop`, `/flag`)

Per Plan #44 decision row #19 (TASTE auto-call), every `manifest.yaml`
slash-command addition lands with a paired changelog entry here and the
reinstall-workflow callout below. Codex had previously caught `/verbosity`
shipping without this; the rule now applies universally to Plan #44.

Plan #44 Bundle E adds three admin-only slash commands to support
incident operations from Slack:

- `/pin <agent> <version>` (Task #10) — Postgres-backed override of the
  agent version pin, persisted to `session_pin_overrides`. Survives
  Railway redeploys (decision row #20).
- `/stop [thread_ts]` (Task #15) — emits `user.interrupt` to halt the
  running session attached to a thread; replies with tokens burned +
  estimated cost + a pre-filled rollback command (decision row #24).
- `/flag <NAME> <value>` (Task #24) — Postgres-backed override for
  in-process flags (`SMOKE_PROBE_LEVEL`, `SF_MCP_VIA_VAULT`,
  `STOP_COMMAND_ENABLED`, `LIMITED_NETWORKING_SHADOW_PCT`,
  `COMPRESSION_ENABLED`). Operator can flip flags at 2am without the
  Railway dashboard (decision row #25).

These commands reuse the existing `commands` scope (granted at initial
bot install 2026-03-01, see scope table above) — **no new scope grant
is required and no workspace reinstall is needed**. The `manifest.yaml`
update that adds the three commands to the `slash_commands` block is a
metadata refresh that Slack picks up the next time the workspace admin
opens the app config; existing `xoxb-` tokens stay valid. Auth is
enforced server-side via `SLACK_ADMIN_USER_IDS` membership (same idiom
as `/refresh-surface`).

If you DO reinstall the app for an unrelated reason (e.g. a future
scope add lands together), confirm in the admin UI that the three new
commands appear under "Slash Commands."

### 2026-05-19 — Plan #49 `member_joined_channel` event subscription

Plan #49 adds a new bot event subscription — `member_joined_channel` —
so the orchestrator can set a channel's `purpose` text the instant the
bot joins, instead of waiting up to 24 hours for the 08:00 PT cron
catch-up. The Slack manifest is updated in both `manifest.yaml` and
`manifest.json`; the handler `handle_member_joined_channel` in
`orchestrator/slack_bot.py` filters on `event["user"] == _bot_user_id`
so only the bot's own joins trigger the description push.

This change is an **event subscription**, NOT a new scope grant.
`groups:write` (channel purpose write on private channels) and
`channels:manage` (write on public channels) are already in the
post-PR-#59 manifest. The only thing missing on the live app is the
event subscription itself — Slack does not auto-propagate new event
subscriptions, so a workspace reinstall is required to activate
delivery of `member_joined_channel` events to the bot.

**Reinstall date and approver**: TBD — coordinated off-hours by the
operator after PR merge. Until reinstall, the 08:00 PT cron is the
sole trigger and continues to function. After reinstall, the
event-driven path activates and channels picked up by future bot
joins receive their purpose within seconds.

Verification after reinstall:

1. Remove the bot from a test channel.
2. Re-add the bot.
3. Within 5 seconds the channel info pane shows the
   `[GTM Health Agent] ...` purpose text.
4. Confirm Railway logs show
   `channel_descriptions: set purpose for <channel>` at the same
   timestamp as the join.

## Reinstall workflow

When this manifest changes scopes, every workspace running the bot
must be reinstalled — Slack does not auto-propagate scope additions.

1. Update `manifest.yaml` in this repo (this file).
2. Add a row to the scope table above.
3. In the Slack admin UI, open the GTM Health Agent app → **Install
   App** → **Reinstall to Workspace**. New scopes appear in the
   consent screen.
4. Admin approves. New `xoxb-` token is issued — rotate
   `SLACK_BOT_TOKEN` on Railway (`fly secrets set` equivalent for
   Railway: redeploy with the new secret).
5. Verify by hitting the new capability: e.g. for canvas scopes,
   re-run `scripts/upsert_slack_channel_canvas.py --dry-run` and
   confirm no `missing_scope` errors.

## Scope-add checklist (for future PRs)

When adding a new scope:

- [ ] Append a row to the table above with `Granted`, `Why`,
      `Source PR`.
- [ ] Add the scope to `manifest.yaml` under
      `oauth_config.scopes.bot`.
- [ ] If the rationale is non-obvious, add a narrative paragraph
      under "Narrative justification."
- [ ] Note in the PR description that workspace admins must
      reinstall the app post-merge.
