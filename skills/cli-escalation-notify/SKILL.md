---
name: cli-escalation-notify
description: Push an out-of-band escalation notification to the user's Slack DM **only when the agent runs in the cursor-agent CLI** (no UI to glance at). In the IDE, returns silently — the user is already watching the chat panel. Use whenever a parent skill is about to ask the user a blocking question (a non-trivial gate, a halt, a confirmation) and the user might not be at the screen. The parent still owns the actual question — this skill only delivers the heads-up.
disable-model-invocation: true
---

# CLI Escalation Notify

## Goal

Tell the user "your agent needs you" via Slack DM when, and only when, the agent is running headless in the cursor-agent CLI. In the Cursor IDE there is no point — the user is by definition looking at the chat panel.

The skill is **fire-and-forget**:
- Detects context, resolves a Slack target, sends one DM.
- Never halts the caller. Any failure (no handle, lookup miss, Slack API error, missing tool) is logged and swallowed.
- Returns a small status object the caller can record for audit.

## Inputs

- `title` (required) — short imperative subject. Example: `iked-test-loop escalation — non-trivial failure`.
- `body_md` (required) — the message body in Slack mrkdwn. The caller composes this — this skill does not template content beyond a small header. ~3–60 lines is the comfortable range; longer messages should be summarised + linked rather than dumped inline.
- `run_context` (optional) — short labelled lines the skill prepends to the body, e.g.
  ```
  run_id: 20260531-091158-e2fb11
  repo: /home/dn/cheetah   host: tyamini-dev2
  branch: SW-265345/feature/v262_routing_ike
  ```
  Pass as a list of `key: value` strings. Skipped when omitted.
- `tail_line` (optional, default `Reply locally via the agent prompt — this Slack message is a push notification, not the answer channel.`) — final reminder line so the user knows where to reply.
- `target_handle_override` (optional) — explicit handle to look up (e.g. `tyamini` or `tom@drivenets.com`). When set, skips the resolution chain and uses this verbatim.

## Hard invariants

- **CLI-only.** When `is_cli_context == false` the skill returns `{status: "skipped", reason: "ide-context"}` without making any tool calls.
- **Non-fatal.** No failure path raises or halts. Worst case is `{status: "skipped", reason: "<why>"}`.
- **One DM per call.** No retries, no fan-out to channels, no @-mentions. If the call fails, the parent's interactive prompt is the canonical fallback.
- **No content authorship.** This skill does not invent text. It prepends a header line and `run_context` lines, then emits `body_md` verbatim, then emits `tail_line`. The parent is responsible for what the user actually reads.

## Workflow

### 1. Detect context

`is_cli_context == true` iff `$CURSOR_AGENT` is set AND **both** `$VSCODE_AGENT_FOLDER` and `$CURSOR_LAYOUT` are unset. The Cursor IDE host always sets the latter two (`CURSOR_LAYOUT=unifiedAgent` for the agent panel); the standalone `cursor-agent` CLI does not. Use `printenv VAR` per variable, not a substring grep, to avoid false positives.

If `is_cli_context == false` → return `{status: "skipped", reason: "ide-context"}`. Stop.

### 2. Resolve the Slack target

If `target_handle_override` was passed, use it verbatim and skip to step 3.

Otherwise, take the first non-empty value from this chain:

1. Env var `CLI_ESCALATION_SLACK_USER` (explicit override for any caller).
2. Env var `IKED_LOOP_SLACK_USER` (legacy override; honored for backward compat).
3. `git config user.email` → local-part (substring before `@`).
4. `git config user.name`.

Record this as `handle`. If all four are empty → return `{status: "skipped", reason: "no-handle"}`.

Look the handle up via `slackbot_slack_find_user(username_or_display_name=<handle>)`. On any error or empty result → return `{status: "skipped", reason: "lookup-failed", handle: "<handle>"}`. On success, capture the returned Slack user ID as `slack_user_id`.

### 3. Compose and send

Build the message body in this order:

```
:rotating_light: <title>
<run_context lines, one per line, if any>

<body_md>

<tail_line>
```

Send via `slackbot_slack_send_msg(channel=<slack_user_id>, message_content=<composed>)`. On any error → return `{status: "send-failed", reason: "<api error>", handle: "<handle>"}`.

On success → return:

```yaml
status: sent
handle: "<handle>"
slack_user_id: "<U01ABC...>"
preview: "<first 80 chars of body_md>"
```

## Output contract

Always returns a small object with `status` set to one of:

- `sent` — DM delivered.
- `skipped` — by design (IDE context, no handle, lookup failed). The parent treats this as success-of-the-skip.
- `send-failed` — Slack API error. The parent treats this the same as `skipped` — fall back to the local interactive prompt.

The parent should **never** branch behaviour on `sent` vs `skipped` — both mean "go ahead and present the local prompt". The status is purely for audit / `meta.json` recording.

## Halt conditions

None. This skill never halts. Every failure is a `skipped` or `send-failed` return.

## Notes for callers

- Do **not** wait for the DM "to be acknowledged" — Slack delivery does not equal user reading. The local interactive prompt remains the source of truth for the user's answer.
- If you want the user to be able to reply via Slack, that's a different feature (interactive Slack bot with state) and out of scope here.
- Keep `body_md` actionable: include the path to the relevant report file and one or two key facts (root cause, target, iteration). The user may be on a phone.
