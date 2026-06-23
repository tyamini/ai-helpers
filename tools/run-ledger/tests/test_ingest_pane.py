#!/usr/bin/env python3
"""ingest-pane parser + vault round-trip test (no network, no pytest needed).

Run: python3 tests/test_ingest_pane.py
Exercises the deterministic producer path end to end:
  synthetic pane.log + verdict.json
    -> run_ledger.py ingest-pane  -> spool.jsonl events
    -> lib.vault.Vault.record     -> per-agent note + run-root note
and asserts the agent tree, model, timing, tool summary, and parent linkage.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import types

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# A minimal, real-shaped cursor-agent stream-json sample. Tool calls emit a
# started + completed line sharing a call_id (so the parser must dedupe), and a
# Task call carries a subagent type (depth-3 reference).
# 2026-01-01T00:00:00Z in epoch-ms; the run spans 15 minutes.
BASE_MS = 1_767_225_600_000
END_MS = BASE_MS + 15 * 60 * 1000


def _pane_lines(sid: str) -> list[str]:
    def tc(call_id, key, ms, subtype, args=None):
        payload = {key: {"args": args or {}, "toolCallId": call_id}}
        return json.dumps({"type": "tool_call", "subtype": subtype,
                           "call_id": call_id, "tool_call": payload,
                           "session_id": sid, "timestamp_ms": ms})
    return [
        json.dumps({"type": "system", "subtype": "init", "session_id": sid,
                    "model": "Opus 4.8 300K High", "timestamp_ms": BASE_MS}),
        json.dumps({"type": "thinking", "text": "planning", "session_id": sid,
                    "timestamp_ms": BASE_MS + 1000}),
        tc("c1", "shellToolCall", BASE_MS + 2000, "started"),
        tc("c1", "shellToolCall", BASE_MS + 3000, "completed"),
        tc("c2", "readToolCall", BASE_MS + 4000, "started"),
        tc("c2", "readToolCall", BASE_MS + 5000, "completed"),
        tc("c3", "editToolCall", BASE_MS + 6000, "started"),
        tc("c3", "editToolCall", BASE_MS + 6500, "completed"),
        tc("c4", "taskToolCall", BASE_MS + 7000, "started",
           args={"subagentType": "reviewer"}),
        tc("c4", "taskToolCall", BASE_MS + 8000, "completed",
           args={"subagentType": "reviewer"}),
        "non-json garbage line that must be skipped",
        json.dumps({"type": "result", "subtype": "success",
                    "session_id": sid, "timestamp_ms": END_MS,
                    "usage": {"outputTokens": 10}}),
    ]


def main() -> int:
    rl = _load("rl", os.path.join(TOOL_DIR, "client", "run_ledger.py"))
    V = _load("vault", os.path.join(TOOL_DIR, "lib", "vault.py"))

    host = socket.gethostname()
    run_id = "20260101-000000-abc123"
    slug = "001-demo-plan"
    sid = "593435e4-d746-4c3f-a166-5a75fb1e740f"

    with tempfile.TemporaryDirectory() as home:
        # Lay out a finished plan under a temp $HOME so _exec_run_dir resolves here.
        os.environ["HOME"] = home
        os.environ["RUN_LEDGER_VAR"] = os.path.join(home, "var")
        os.environ["RUN_LEDGER_URL"] = "http://127.0.0.1:1"  # unreachable -> spool kept
        plan_dir = os.path.join(home, ".exec-runs", run_id, "plans", slug)
        os.makedirs(plan_dir, exist_ok=True)
        with open(os.path.join(plan_dir, "pane.log"), "w") as fh:
            fh.write("\n".join(_pane_lines(sid)) + "\n")
        with open(os.path.join(plan_dir, "verdict.json"), "w") as fh:
            json.dump({"status": "complete", "committed": True,
                       "head_sha": "deadbeef", "chat_id": sid}, fh)

        args = types.SimpleNamespace(run_id=run_id, slug=slug, repo="")
        rl.cmd_ingest_pane(args)

        # --- spool assertions: ingest-pane emits the agent node only ----------
        spool = os.path.join(home, "var", "spool.jsonl")
        events = [json.loads(ln) for ln in open(spool) if ln.strip()]
        by_event = {e["event"]: e for e in events}
        assert set(by_event) == {"subagent_start", "subagent_stop"}, by_event

        start = by_event["subagent_start"]
        assert start["session_id"] == sid
        assert start["parent_session_id"] == run_id      # links agent -> run root
        assert start["plan"] == slug
        assert start["model"] == "Opus 4.8 300K High"
        assert start["ts"] == rl._ts_from_ms(BASE_MS)     # first timestamp_ms
        assert start["run_id"] == run_id and start["host"] == host

        stop = by_event["subagent_stop"]
        assert stop["ts"] == rl._ts_from_ms(END_MS)        # last timestamp_ms
        assert stop["status"] == "complete"
        assert stop["tool_counts"] == {"Shell": 1, "Read": 1, "Edit": 1, "Task": 1}
        assert stop["tools"] == "Edit=1 Read=1 Shell=1 Task=1"
        assert stop["subagents"] == "reviewer"

        # --- vault round-trip -------------------------------------------------
        # A milestone (as the executor's notify path emits it) creates the
        # run-root note keyed by run_id; the agent node parents to it.
        milestone = {"event_uuid": "m1", "ts": rl._ts_from_ms(BASE_MS),
                     "host": host, "run_id": run_id, "source": "notify",
                     "event": "plan_finish", "plan": slug, "committed": "true"}
        vault = V.Vault(os.path.join(home, "vault"))
        for e in events + [milestone]:
            vault.record(e)

        agent = vault.get_run(host, run_id)
        assert agent is not None
        notes = {a.get("_key"): a for a in agent["agents"]}   # _key = session_id or run_id
        assert sid in notes, notes
        an = notes[sid]
        assert an["model"] == "Opus 4.8 300K High"
        assert an["status"] == "complete"
        assert an["started_at"] == rl._ts_from_ms(BASE_MS)
        assert an["ended_at"] == rl._ts_from_ms(END_MS)
        assert an["tools"] == {"Shell": 1, "Read": 1, "Edit": 1, "Task": 1}
        assert an["counts"]["tool_calls"] == 4
        assert an["parent"] == f"[[{host}/{run_id}]]"      # parent wikilink to run root

        # The run-root note (keyed by run_id) carries the plan_finish milestone.
        root = notes.get(run_id)
        assert root is not None, "run-root note missing"
        events_for_root = V._read_events(vault._events_path(host, run_id))
        assert any(e["event"] == "plan_finish" for e in events_for_root)

    print("OK: ingest-pane parser + vault round-trip passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
