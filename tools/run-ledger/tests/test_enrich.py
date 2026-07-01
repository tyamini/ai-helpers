#!/usr/bin/env python3
"""enrich distiller + rename tests (no network, no pytest).
Run: python3 tests/test_enrich.py
"""
from __future__ import annotations
import importlib.util, json, os, socket, sys, tempfile, types

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rename_agent(V):
    host = "h1"
    with tempfile.TemporaryDirectory() as root:
        vault = V.Vault(root)
        # seed a run-root note + one agent note via record()
        run_id = "20260101-000000-abc"
        sid = "sess-1111"
        vault.record({"event_uuid": "e1", "ts": "2026-01-01T00:00:00Z", "host": host,
                      "run_id": run_id, "source": "notify", "event": "plan_finish",
                      "plan": "001-demo"})                       # run-root note
        vault.record({"event_uuid": "e2", "ts": "2026-01-01T00:00:10Z", "host": host,
                      "run_id": run_id, "source": "ledger", "event": "subagent_start",
                      "session_id": sid, "parent_session_id": run_id, "role": "subagent"})
        # rename the agent note
        assert vault.rename_agent(host, sid, f"{run_id}__001-demo__subagent") is True
        old = vault._agent_path(host, sid)
        new = vault._agent_path(host, f"{run_id}__001-demo__subagent")
        assert not os.path.exists(old) and os.path.exists(new)
        fm, _ = V.parse_note(open(new).read())
        assert f"{host}/{sid}" in fm["aliases"]                 # old link still resolves
        assert fm["session_id"] == sid                          # field preserved
        # sidecars moved
        assert os.path.exists(vault._events_path(host, f"{run_id}__001-demo__subagent"))
        assert not os.path.exists(vault._events_path(host, sid))
        # idempotent: second call is a no-op (source already gone)
        assert vault.rename_agent(host, sid, f"{run_id}__001-demo__subagent") is False
    print("OK: rename_agent")


def test_distill(E):
    sid = "sess-2222"
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": sid,
                    "model": "Opus 4.8 300K High", "timestamp_ms": 1000}),
        json.dumps({"type": "user", "session_id": sid, "message": {"role": "user",
                    "content": [{"type": "text", "text": "DISPATCH PROMPT HERE"}]}}),
        json.dumps({"type": "thinking", "text": "secret monologue", "timestamp_ms": 1500}),
        json.dumps({"type": "assistant", "timestamp_ms": 2000, "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "I'll read the plan."}]}}),
        json.dumps({"type": "tool_call", "subtype": "completed", "call_id": "c0",
                    "timestamp_ms": 2050, "tool_call": {"readToolCall": {
                        "args": {"path": "/x/.ai/skills/common/implementation-loop/SKILL.md"},
                        "result": {"success": {"totalLines": 1}}}}}),
        json.dumps({"type": "tool_call", "subtype": "completed", "call_id": "c1",
                    "timestamp_ms": 2100, "tool_call": {"shellToolCall": {
                        "args": {"command": "make test"},
                        "result": {"success": {"output": "X" * 5000}}}}}),
        json.dumps({"type": "user", "session_id": sid, "message": {"role": "user",
                    "content": [{"type": "text", "text": "INJECTED DIRECTIVE: fix it"}]}}),
        json.dumps({"type": "assistant", "timestamp_ms": 3000, "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "Done.\n```yaml\nloop_report:\n  exit_reason: met-criteria\n```"}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "final", "usage": {"outputTokens": 1}}),
    ]
    d = E.distill_stream(lines)
    body = d["transcript"]
    assert "secret monologue" not in body                       # thinking dropped
    assert "I'll read the plan." in body                        # assistant kept
    assert "Shell" in body and "make test" in body              # tool call kept
    assert "[truncated" in body                                 # big output truncated
    assert d["prompt"] == "DISPATCH PROMPT HERE"                # first user msg
    assert d["directives"] == ["INJECTED DIRECTIVE: fix it"]    # later user msgs
    assert "loop_report:" in d["final_summary"]                 # last assistant text
    assert "implementation-loop" in d["harness"]["skills"]      # SKILL.md read
    assert "make" in d["harness"]["scripts"]                    # shell invocation
    print("OK: distill_stream")


def test_prep(E):
    host = socket.gethostname()
    run_id = "20260101-000000-prep"
    slug = "001-demo"
    sid = "sess-3333"
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        plan_dir = os.path.join(home, ".exec-runs", run_id, "plans", slug)
        os.makedirs(plan_dir)
        open(os.path.join(plan_dir, "pane.log"), "w").write("\n".join([
            json.dumps({"type": "system", "subtype": "init", "session_id": sid,
                        "model": "M", "timestamp_ms": 1000}),
            json.dumps({"type": "user", "session_id": sid, "message": {"role": "user",
                        "content": [{"type": "text", "text": "PROMPT"}]}}),
            json.dumps({"type": "assistant", "timestamp_ms": 2000, "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "hi"}]}}),
        ]) + "\n")
        json.dump({"status": "complete", "chat_id": sid},
                  open(os.path.join(plan_dir, "verdict.json"), "w"))
        json.dump({"run_id": run_id, "plans": [slug]},
                  open(os.path.join(home, ".exec-runs", run_id, "meta.json"), "w"))
        m = E.build_manifest(run_id)
        assert m["host"] == host
        rootrec = [a for a in m["agents"] if a["role"] == "executor"]
        planrec = [a for a in m["agents"] if a["role"] == "subagent"]
        assert rootrec and rootrec[0]["new_name"] == f"{run_id}__executor"
        assert planrec and planrec[0]["old_key"] == sid
        assert planrec[0]["new_name"] == f"{run_id}__{slug}__subagent"
        # prep writes distilled transcript files
        E.cmd_prep(types.SimpleNamespace(run_id=run_id))
        enrich_dir = os.path.join(home, ".exec-runs", run_id, "enrich")
        assert os.path.exists(os.path.join(enrich_dir, "manifest.json"))
        assert os.path.exists(os.path.join(enrich_dir, f"{slug}.distilled.md"))
    print("OK: prep")


def test_roundtrip(V, E):
    import http.server, threading
    host = socket.gethostname()
    run_id = "20260101-000000-rt"
    slug = "001-demo"
    sid = "sess-4444"
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        os.environ["RUN_LEDGER_VAULT"] = os.path.join(home, "vault")
        os.environ["RUN_LEDGER_VAR"] = os.path.join(home, "var")
        # seed vault agent note for the plan agent
        vault = V.Vault(os.environ["RUN_LEDGER_VAULT"])
        vault.record({"event_uuid": "e1", "ts": "2026-01-01T00:00:00Z", "host": host,
                      "run_id": run_id, "source": "ledger", "event": "subagent_start",
                      "session_id": sid, "parent_session_id": run_id, "role": "subagent"})
        # lay out exec-run + prep
        plan_dir = os.path.join(home, ".exec-runs", run_id, "plans", slug)
        os.makedirs(plan_dir)
        open(os.path.join(plan_dir, "pane.log"), "w").write(json.dumps(
            {"type": "system", "subtype": "init", "session_id": sid,
             "model": "M", "timestamp_ms": 1000}) + "\n")
        json.dump({"status": "complete", "chat_id": sid},
                  open(os.path.join(plan_dir, "verdict.json"), "w"))
        json.dump({"run_id": run_id, "plans": [slug]},
                  open(os.path.join(home, ".exec-runs", run_id, "meta.json"), "w"))
        E.cmd_prep(types.SimpleNamespace(run_id=run_id))
        # start server in-process on an ephemeral port
        app = _load("app", os.path.join(TOOL_DIR, "server", "app.py"))
        app.VAULT = V.Vault(os.environ["RUN_LEDGER_VAULT"])
        srv = http.server.HTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["RUN_LEDGER_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
        # rename + archive over HTTP
        E.cmd_rename(types.SimpleNamespace(run_id=run_id))
        E.cmd_put_archive(types.SimpleNamespace(run_id=run_id))
        srv.shutdown()
        # assertions: note renamed, archive note exists linking the agent
        new = vault._agent_path(host, f"{run_id}__{slug}__subagent")
        assert os.path.exists(new), "renamed note missing"
        edir = os.path.join(os.environ["RUN_LEDGER_VAULT"], "enrichment")
        files = os.listdir(edir)
        assert any(f.endswith(f"transcript-{slug}.md") for f in files), files
        arch = open(os.path.join(edir, [f for f in files if "transcript" in f][0])).read()
        assert f"[[{host}/{run_id}__{slug}__subagent]]" in arch
    print("OK: roundtrip")


def main():
    V = _load("vault", os.path.join(TOOL_DIR, "lib", "vault.py"))
    test_rename_agent(V)
    E = _load("enrich", os.path.join(TOOL_DIR, "client", "enrich.py"))
    test_distill(E)
    test_prep(E)
    test_roundtrip(V, E)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
