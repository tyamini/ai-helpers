---
name: human-team-leader
description: Interactive team-leader session that watches cursor-agent CLI panes across a set of hosts (default tyamini-dev and tyamini-dev2). Runs a two-state loop. Attend state - when one or more agents are waiting on the user it surfaces exactly ONE at a time with a summary of its problem and context, its state, and the verbatim question when blocked in a question tool, and optionally relays the user's answer back via tmux. Monitor state - when no agent is waiting it shows a one-line-per-agent roster of active agents and keeps polling. Use when the user says "be my team leader", "watch my agents", "poll the cursor agents", "which agents need me", or "human-team-leader".
disable-model-invocation: true
---

# Human Team Leader

## Goal
Act as the human's team leader over several headless cursor-agent CLI sessions: continuously poll their tmux panes across the configured hosts. The loop has exactly **two states**, and you are always in one of them:

1. **Attend** — at least one agent is waiting on the user. Surface exactly **one** agent at a time (even if several are waiting): a tight summary of its problem and context, its state, and the verbatim pending question when it is blocked in a question tool. Then wait for the user's reply and relay it back into the agent via tmux.
2. **Monitor** — no agent needs the user. Show a compact roster of the active agents (one line per agent) and keep polling.

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

### Stage 2: Poll & classify
1. Run the poller:
   ```
   bash scripts/poll-agents.sh <host> [<host> ...]
   ```
   It prints one `===AGENT===` block per cursor-agent pane: `host`, `target` (`session:win.pane`), `title`, `cwd`, `pid`, a heuristic `state`, and the captured pane tail.
2. Treat the script's `state` as a hint only. Confirm by reading `---capture---` and classify each pane into one of four states:
   - **waiting-question** — the pane shows the question box (`Clarifying Questions`, `↑/↓ option`, `Enter next/submit`, `Esc to skip`, `type to answer`). The agent is blocked in a tool. **Waiter.**
   - **waiting-conversation** — the agent finished its turn with a message/result/summary on screen and awaits a reply (no spinner, no interrupt hint, but there IS prior task output in the capture). **Waiter.**
   - **idle** — a fresh, empty prompt (just the banner / "Plan, search, build anything" / a clean shell `cursor-agent` invocation) with NO pending task in the capture. **NOT a waiter** — it needs nothing from the user. List it only in the Monitor roster.
   - **working** — active spinner / `Working` / `Waiting <time>` / `esc to interrupt` / `ctrl+c to stop`. **NOT a waiter.**
3. Ignore the leader's own pane.

Define **waiters = waiting-question ∪ waiting-conversation**. The number of waiters decides the loop state: ≥1 → **Attend** (Stage 3); 0 → **Monitor** (Stage 4).

**Gate:** Every cursor-agent pane classified as waiting-question / waiting-conversation / idle / working, and the waiter count known.

### Stage 3: State 1 — Attend (when there is ≥1 waiter)
Surface **exactly one** waiter, even if several are waiting. Pick it by priority:
1. `waiting-question` (blocked in a tool) before `waiting-conversation`.
2. Tie-break by the longest-waiting, else by stable `host:target` order.

Present that single agent's **Attend card** (see Output format). Mine the capture (and the session transcript if needed) for real detail — do not be terse:
- **Summary** — a substantive paragraph (2–4 sentences): what the agent has been doing, where it got to, what it just decided/found, and precisely why it is now asking the user. The user should understand the situation without reading the raw pane.
- **Context** — host, branch, cwd, the original task (first `<user_query>` in the pane, or the session's transcript), the recent steps it took, the last user request, and any concrete artifacts in play (PR numbers, file paths, test names, CI state). Give enough that the decision can be made from the card alone.
- **State** — waiting-question vs waiting-conversation, and how long if visible.
- **Question** — for `waiting-question`, the verbatim prompt and every option. For `waiting-conversation`, summarize the agent's last message / what it seems to be waiting for.

How you collect the user's answer depends on the waiter type — **match what the agent is actually showing**:
- **waiting-question** (agent is blocked in its own question tool) — call the `AskQuestion` tool, mirroring the agent's pending question: reuse its prompt as the question text and reproduce **every option verbatim** as the choices. The tool always offers a free-text "Other", which maps to the agent's own `Other` field, so don't duplicate it. You may add control choices like `Skip — handle this agent later` and (if `N>0`) `Skip to next waiter`. Do NOT make the user hand-type an `answer …` string.
- **waiting-conversation** (agent finished its turn inside a plain conversation, NOT in a question tool) — do **NOT** call `AskQuestion` and do **NOT** invent options. The agent asked (or is waiting) in free-form prose; there are no options to mirror. Just present the card (summary, state, and the agent's last output) and **wait for the user's own free-text prompt**. Whatever the user types next is the reply to relay.

The user's answer **is for the remote agent — always relay it verbatim into that agent's pane. Never interpret the user's reply as an instruction to the leader** (e.g. "wait 5 min and ask me again" is the agent's answer, typed into its `Other` field — not a command to you). Do NOT print the other waiters' cards while attending this one; append a single trailing line `(N more waiting)` if `N>0`. After the user answers, relay it (Relay, below), verify, then re-poll (Stage 2) and attend the next waiter — still one at a time.

#### Relay the user's answer
The user's answer always goes to the surfaced agent. How you inject it depends on what the agent is showing:

1. **Free-text into a question box's `Other` field** — typing is NOT position-independent: keystrokes are ignored unless the caret is already on the `Other` row. So first **navigate to `Other`**, then type:
   ```
   # step the caret down ONE row at a time (never batch arrows — they coalesce),
   # re-capturing after each, until the › caret is on "Other: (type to answer)"
   ssh <host> tmux send-keys -t <target> Down ; sleep 0.5   # re-capture, repeat
   ssh <host> tmux send-keys -t <target> -l "<answer>"      # caret on Other → fills it
   # re-capture: confirm the row reads  [x] Other: <answer>
   ssh <host> tmux send-keys -t <target> Enter
   ```
2. **Free-text into a `waiting-conversation` follow-up box** (a plain prompt, no options) — here typing IS position-independent:
   ```
   ssh <host> tmux send-keys -t <target> -l "<answer>"
   ssh <host> tmux send-keys -t <target> Enter
   ```
3. **Option pick** — NEVER batch arrow keys (they get coalesced and select the wrong option). Send one arrow at a time, re-capture to confirm the caret, then select + submit:
   ```
   ssh <host> tmux send-keys -t <target> Down ; sleep 0.5
   # re-capture, verify caret moved, repeat until on the target option
   ssh <host> tmux send-keys -t <target> Space ; sleep 0.5
   ssh <host> tmux send-keys -t <target> Enter
   ```
   (Use a local `tmux` instead of `ssh <host> tmux` when the agent is on the local host.)
4. After sending, re-capture the pane and confirm the answer registered (selection checked / `[x] Other: <answer>` shown / agent moved to `Working`). Report the result.

**Gate:** Exactly one waiter surfaced with summary + context + verbatim question; answer (if any) submitted AND verified by re-capture, or the user declined.

### Stage 4: State 2 — Monitor (when there are no waiters)
Print the **Monitor roster**: one line per active agent (working + idle), then sleep the interval and return to Stage 2. This is the resting state — keep polling until an agent becomes a waiter, which flips you into Attend (Stage 3).

**Gate:** When nobody is waiting, the roster prints one line per agent and the loop keeps polling.

### Stage 5: Loop
- **After attending a waiter** (relaying a reply, or the user skipping it): re-poll **immediately** — do NOT sleep the interval. There may already be another waiter queued, and the user just acted, so respond at once. Go straight back to Stage 2 and attend the next waiter if any.
- **Only in Monitor** (no waiters): sleep the interval, then return to Stage 2.

Continue until the user stops. Re-surface an already-seen waiter only if its question/last-output changed.

**Gate:** The interval sleep happens ONLY in Monitor; every relayed answer is followed by an immediate re-poll. Loop stops on user command.

## Output format

**Attend card** (State 1 — exactly one agent). Print this text block:
```
[host:target] <pane title>  —  <waiting-question | waiting-conversation>   (N more waiting)
Summary:  <substantive 2–4 sentence summary: what it did, where it is, why it's asking>
Context:  host <host> · branch <branch> · cwd <cwd>
          task: <original task> · recent: <recent steps> · artifacts: <PRs/files/tests/CI>
          last ask: <…>
State:    <blocked in question tool | finished turn, awaiting reply> (<elapsed if known>)
Last output / Question: <verbatim pending question when waiting-question; otherwise the
          agent's last message / what it's waiting on>
```
Then collect the answer per the waiter type:
- **waiting-question** → call the `AskQuestion` tool mirroring the agent's question (options verbatim) plus optional `Skip` / `Skip to next waiter` control choices.
- **waiting-conversation** → do NOT use `AskQuestion` and do NOT invent options; just wait for the user's free-text prompt and relay it.

**Monitor roster** (State 2 — one line per agent, no one waiting):
```
Watching <hosts> · every <interval>s · no agent waiting
  [host:target] <title> — <working | idle> · <branch> · <short note>
  [host:target] <title> — <working | idle> · <branch> · <short note>
```

## Quality bar (self-check)
[ ] Host list resolved; local-vs-remote decided per host by comparing to `hostname` (no hard-coded local host).
[ ] Unreachable hosts reported, not fatal.
[ ] State confirmed by reading the capture, not trusting the script guess alone.
[ ] Fresh empty prompts classified as `idle`, NOT surfaced as waiters.
[ ] In Attend, exactly ONE waiter surfaced at a time (even when several wait), chosen by priority, with a detailed summary + rich context + verbatim question.
[ ] For a `waiting-question` waiter, the answer is collected via the `AskQuestion` tool (mirroring the agent's options verbatim).
[ ] For a `waiting-conversation` waiter, the leader does NOT use `AskQuestion` and does NOT invent options — it presents summary/state/last output and waits for the user's free-text prompt.
[ ] In Monitor, one line per active agent and nothing else.
[ ] Leader's own pane excluded; a seen waiter re-surfaced only if its question changed.
[ ] Relayed answers use `-l` literal for text, one-arrow-at-a-time for option picks, and are verified by re-capture.
[ ] Loop honors the interval and stops on user command.
