---
name: human-team-leader
description: Interactive team-leader session that watches cursor-agent CLI panes across a set of hosts (default tyamini-dev and tyamini-dev2) and surfaces any agent that has stopped working and is waiting on the user. For each waiting agent it presents a short context summary, the current state, and the actual question text when the agent is blocked in a question tool. Optionally relays the user's answer back into the agent via tmux. Use when the user says "be my team leader", "watch my agents", "poll the cursor agents", "which agents need me", or "human-team-leader".
disable-model-invocation: true
---

# Human Team Leader

## Goal
Act as the human's team leader over several headless cursor-agent CLI sessions: continuously poll their tmux panes across the configured hosts and, whenever one is waiting for the user (blocked in a question tool, or idle after finishing its turn), present a tight summary — context, state, and the actual pending question — and optionally relay the user's answer back.

## Key facts (read first)
- A pending question is **never flushed to disk** (not to the agent-transcripts `*.jsonl`, not to `store.db`). The reliable way to read it is to **scrape the agent's tmux pane** with `tmux capture-pane`. This is the core technique of this skill.
- The leader can also **answer** an agent by writing keystrokes to its pane with `tmux send-keys` — this is the only supported cross-agent answer path.
- **Do not assume which host is local.** For each host, if its name matches the local `hostname`, poll with a local `tmux`; otherwise poll over `ssh <host>`. The bundled script handles this.

## Workflow

### Stage 1: Establish watch scope
1. Determine the host list. Default: `tyamini-dev tyamini-dev2`. Honor a user-supplied list or `POLL_HOSTS`.
2. Determine poll interval (default 20s) and confirm the user wants a continuous loop (vs a single sweep).
3. Verify reachability: every non-local host must answer `ssh -o BatchMode=yes <host> true`. Report any host that fails and continue with the reachable ones.

**Gate:** Host list fixed and at least one host reachable.

### Stage 2: Poll cycle
1. Run the poller:
   ```
   bash scripts/poll-agents.sh <host> [<host> ...]
   ```
   It prints one `===AGENT===` block per cursor-agent pane: `host`, `target` (`session:win.pane`), `title`, `cwd`, `pid`, a heuristic `state`, and the captured pane tail.
2. Treat the script's `state` as a hint only. Confirm by reading `---capture---`:
   - **waiting-question** — the pane shows the question box (`Clarifying Questions`, `↑/↓ option`, `Enter next/submit`, `Esc to skip`, `type to answer`). The agent is blocked.
   - **working** — active spinner / `Working` / `Waiting <time>` / `esc to interrupt` / `ctrl+c to stop`. NOT waiting — skip it.
   - **waiting-conversation** — idle prompt, no spinner and no interrupt hint: the agent finished its turn and awaits a reply.
3. Ignore the leader's own pane.

**Gate:** Every cursor-agent pane classified as working / waiting-question / waiting-conversation.

### Stage 3: Surface waiting agents
For each agent in `waiting-question` or `waiting-conversation` that has NOT already been surfaced this run, present one card (see Output format). Extract from the capture:
- **Context** — branch/cwd from the prompt line, plus the original task (first `<user_query>` in the pane, or the session's transcript) and the last user request.
- **State** — waiting-question vs waiting-conversation, and how long if visible.
- **Question** — for `waiting-question`, the verbatim prompt and every option (including `Other`). For `waiting-conversation`, summarize the agent's last message / what it seems to be waiting for.

Track surfaced agents (by `host+target`) so the loop does not re-spam an unchanged waiter.

**Gate:** Each newly-waiting agent presented exactly once with context, state, and question.

### Stage 4: Relay the user's answer (optional)
Only when the user dictates an answer for a specific agent:
1. **Free-text** (the `Other` field, or a `waiting-conversation` follow-up box) — position-independent, prefer this:
   ```
   tmux send-keys -t <target> -l "<answer>"      # local
   ssh <host> tmux send-keys -t <target> -l "<answer>"   # remote
   tmux send-keys -t <target> Enter
   ```
2. **Option pick** — NEVER batch arrow keys (they get coalesced and select the wrong option). Send one arrow at a time, re-capture to confirm the caret, then submit:
   ```
   tmux send-keys -t <target> Up ; sleep 0.4
   # re-capture, verify caret moved, repeat until on the target option
   tmux send-keys -t <target> Space ; sleep 0.4
   tmux send-keys -t <target> Enter
   ```
3. After sending, re-capture the pane and confirm the answer registered (selection checked / agent moved to `working`). Report the result.

**Gate:** Answer submitted AND verified by re-capture, or the user declined to answer.

### Stage 5: Loop
Sleep the interval, then return to Stage 2. Continue until the user stops. On each cycle, only surface agents that are newly waiting or whose question changed.

**Gate:** Loop continues until the user ends the session.

## Output format
One card per waiting agent:
```
[host:target] <pane title>  —  <waiting-question | waiting-conversation>
Context: <branch/cwd> · <one-line task summary> · last ask: <…>
State:   <blocked in question tool | finished turn, awaiting reply> (<elapsed if known>)
Question:
  <verbatim prompt>
    - <option 1>
    - <option 2>
    - Other: (free text)
Reply:   say e.g. "answer host:target <option/text>" and I'll relay it.
```
When no agent is waiting, report a single status line with counts per host (e.g. `tyamini-dev: 2 working · tyamini-dev2: 1 working, 1 waiting`).

## Quality bar (self-check)
[ ] Host list resolved; local-vs-remote decided per host by comparing to `hostname` (no hard-coded local host).
[ ] Unreachable hosts reported, not fatal.
[ ] State confirmed by reading the capture, not trusting the script guess alone.
[ ] Question text and ALL options reproduced verbatim for waiting-question agents.
[ ] Leader's own pane excluded; agents surfaced once until their state changes.
[ ] Relayed answers use `-l` literal for text, one-arrow-at-a-time for option picks, and are verified by re-capture.
[ ] Loop honors the interval and stops on user command.
