#!/usr/bin/env python3
"""run-ledger client: record / ingest-pane / flush / timeline.

Runs on every machine. ``record`` and ``ingest-pane`` are **fail-open**: they
never raise and always exit 0, so a telemetry problem can never block an agent.

Producers are deterministic and hook-free. Events come from a run's own
artifacts: milestone events (``run_start``/``plan_start``/``plan_finish``/
``run_complete``) carry a ``run_id`` and route to a single run-root note, and
``ingest-pane`` turns a finished per-plan ``pane.log`` (cursor-agent stream-json)
plus its ``verdict.json`` into that agent's node (keyed by its ``session_id``,
parented to the ``run_id``). There is no Cursor hook and no session registry.

Commands
--------
- record       build an event, append to the local spool, kick a flush
- ingest-pane  parse a finished plan's pane.log + verdict.json into events
- flush        forward spooled events to the central service; drop acked, keep rest
- rebuild      regenerate vault notes from their event sidecars (central host)
- timeline     render a deterministic per-run summary (central API, vault, or spool)

Config (env)
------------
- RUN_LEDGER_URL      central base URL (default http://tyamini-dev:8723)
- RUN_LEDGER_TOKEN    bearer token (default empty -> no auth header)
- RUN_LEDGER_VAR      var dir (default <tool>/var)
- RUN_LEDGER_VAULT    server vault dir, for local timeline reads on tyamini-dev
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)

DEFAULT_URL = "http://tyamini-dev:8723"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _var_dir() -> str:
    return os.environ.get("RUN_LEDGER_VAR") or os.path.join(TOOL_DIR, "var")


def _spool_path() -> str:
    return os.path.join(_var_dir(), "spool.jsonl")


def _lock_path() -> str:
    return os.path.join(_var_dir(), "flush.lock")


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _host() -> str:
    return socket.gethostname()


def _url() -> str:
    return (os.environ.get("RUN_LEDGER_URL") or DEFAULT_URL).rstrip("/")


def _auth_headers() -> dict:
    token = os.environ.get("RUN_LEDGER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


# --------------------------------------------------------------------------- #
# spool
# --------------------------------------------------------------------------- #
def _spool_append(event: dict) -> None:
    os.makedirs(_var_dir(), exist_ok=True)
    with open(_spool_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _spawn_flush() -> None:
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "flush"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass  # flush is best-effort; the next record (or run end) will retry


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
def _parse_fields(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = v
    return out


def _record_event(source: str, name: str, run_id: str, fields: dict) -> int:
    """Build one event and append it to the spool. Always returns 0.

    Every event carries an explicit ``run_id`` (milestone events) or is keyed by
    its ``session_id`` (agent-node events from ingest-pane), so this just needs a
    run_id to attribute to. A field named ``ts`` overrides the default timestamp,
    which is how ingest-pane stamps real per-agent start/end times.
    """
    try:
        name = (name or "").replace("-", "_")
        source = source or "notify"
        run_id = run_id or fields.get("run_id") or ""
        if not run_id:
            return 0  # nothing to attribute to

        event = {
            "event_uuid": str(uuid.uuid4()),
            "ts": _now(),
            "host": _host(),
            "run_id": run_id,
            "source": source,
            "event": name,
        }
        for k, v in fields.items():
            if k not in ("run_id", "host"):
                event[k] = v

        _spool_append(event)
        _spawn_flush()
    except Exception:
        pass  # fail-open: telemetry must never break the caller
    return 0


def cmd_record(args) -> int:
    fields = _parse_fields(args.field)
    name = args.event or fields.pop("event", "")
    source = args.source or fields.pop("source", "notify")
    run_id = args.run_id or fields.get("run_id") or ""
    return _record_event(source, name, run_id, fields)


# --------------------------------------------------------------------------- #
# ingest-pane (turn a finished per-plan pane.log + verdict into events)
# --------------------------------------------------------------------------- #
def _exec_run_dir(run_id: str) -> str:
    return os.path.join(os.path.expanduser("~/.exec-runs"), run_id)


def _ts_from_ms(ms) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime(_TS_FMT)
    except Exception:
        return ""


def _iter_stream(path: str):
    """Yield parsed stream-json objects from a pane.log (best-effort)."""
    if not os.path.exists(path):
        return
    try:
        fh = open(path, errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(o, dict):
                yield o


def _tool_label(tc: dict):
    """Derive a tool label from a tool_call payload, defensively.

    Real shape (cursor-agent stream-json): a single-key dict like
    ``{"shellToolCall": {...}}`` -> ``Shell``. Falls back to common name fields.
    """
    if not isinstance(tc, dict):
        return None
    for k in ("name", "tool", "toolName", "tool_name"):
        if tc.get(k):
            return str(tc[k])
    for k in tc.keys():
        if k.endswith("ToolCall"):
            base = k[: -len("ToolCall")]
            return (base[:1].upper() + base[1:]) if base else k
    return next(iter(tc.keys()), None)


def _task_subagent_type(tc: dict):
    """Best-effort subagent type from a Task tool_call (depth-3 reference)."""
    payload = tc.get("taskToolCall") if isinstance(tc, dict) else None
    payload = payload if isinstance(payload, dict) else (tc if isinstance(tc, dict) else {})
    args = payload.get("args") if isinstance(payload.get("args"), dict) else payload
    if isinstance(args, dict):
        for k in ("subagentType", "subagent_type", "agentType", "agent"):
            if args.get(k):
                return str(args[k])
    return None


def _parse_pane(path: str) -> dict:
    """Extract a deterministic summary of one per-plan agent's stream."""
    session_id = model = None
    first_ts = last_ts = None
    tool_counts: dict = {}
    subagents: list = []
    seen_calls: set = set()
    for o in _iter_stream(path):
        if session_id is None:
            session_id = o.get("session_id") or o.get("sessionId")
        if model is None and o.get("model"):
            model = o.get("model")
        ms = o.get("timestamp_ms")
        if isinstance(ms, (int, float)):
            if first_ts is None:
                first_ts = ms
            last_ts = ms
        if o.get("type") == "tool_call":
            call_id = o.get("call_id") or o.get("toolCallId")
            if call_id and call_id in seen_calls:
                continue  # count each call once (started/completed share a call_id)
            if call_id:
                seen_calls.add(call_id)
            label = _tool_label(o.get("tool_call") or {})
            if label:
                tool_counts[label] = tool_counts.get(label, 0) + 1
                if label == "Task":
                    st = _task_subagent_type(o.get("tool_call") or {})
                    if st:
                        subagents.append(st)
    return {
        "session_id": session_id,
        "model": model,
        "started_at": _ts_from_ms(first_ts) if first_ts is not None else "",
        "ended_at": _ts_from_ms(last_ts) if last_ts is not None else "",
        "tool_counts": tool_counts,
        "subagents": subagents,
    }


def _fmt_counts(counts: dict) -> str:
    return " ".join(f"{k}={counts[k]}" for k in sorted(counts))


def _read_verdict(plan_dir: str) -> dict:
    try:
        with open(os.path.join(plan_dir, "verdict.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def cmd_ingest_pane(args) -> int:
    """Parse a finished plan's pane.log + verdict.json into the agent node. Fail-open.

    Emits, for one per-plan agent, ``subagent_start`` / ``subagent_stop`` keyed by
    the agent's ``session_id`` and parented to the ``run_id``, carrying model,
    machine, real start/end timestamps, a per-tool summary, and any depth-3 Task
    subagents observed. Plan/run milestones are not emitted here — they flow
    through the executor's cli-escalation-notify calls (and the Stage-2 run-start
    line), so this never duplicates the run-root timeline.
    """
    try:
        run_id = args.run_id
        slug = args.slug
        plan_dir = os.path.join(_exec_run_dir(run_id), "plans", slug)
        pane_log = os.path.join(plan_dir, "pane.log")
        verdict = _read_verdict(plan_dir)
        s = _parse_pane(pane_log)
        sid = s["session_id"] or verdict.get("chat_id")
        if not sid:
            return 0  # nothing identifiable to attribute the node to

        # parent_session_id=run_id links the agent note to the run-root note.
        start_fields = {"session_id": sid, "parent_session_id": run_id,
                        "role": "subagent", "plan": slug}
        if s["model"]:
            start_fields["model"] = s["model"]
        if s["started_at"]:
            start_fields["ts"] = s["started_at"]
        _record_event("ledger", "subagent_start", run_id, start_fields)

        stop_fields = {"session_id": sid}
        if verdict.get("status"):
            stop_fields["status"] = verdict["status"]
        if s["ended_at"]:
            stop_fields["ts"] = s["ended_at"]
        if s["tool_counts"]:
            stop_fields["tool_counts"] = s["tool_counts"]        # dict -> frontmatter
            stop_fields["tools"] = _fmt_counts(s["tool_counts"])  # str -> timeline line
        if s["subagents"]:
            stop_fields["subagents"] = ",".join(s["subagents"])
        _record_event("ledger", "subagent_stop", run_id, stop_fields)
    except Exception:
        pass  # fail-open: telemetry must never break the caller
    return 0


# --------------------------------------------------------------------------- #
# flush
# --------------------------------------------------------------------------- #
def cmd_flush(args) -> int:
    """Forward spooled events; drop acked, keep the rest. Best-effort, exit 0."""
    import fcntl

    try:
        import requests  # local import keeps `record` cheap
    except Exception:
        return 0

    spool = _spool_path()
    if not os.path.exists(spool):
        return 0

    os.makedirs(_var_dir(), exist_ok=True)
    lock_fd = os.open(_lock_path(), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another flush already running

        with open(spool, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        if not lines:
            return 0

        events = []
        for ln in lines:
            try:
                events.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        if not events:
            open(spool, "w").close()
            return 0

        try:
            resp = requests.post(
                _url() + "/events",
                json={"events": events},
                headers=_auth_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            acked = set(resp.json().get("acked", []))
        except Exception:
            return 0  # central unreachable -> keep spool intact, retry later

        remaining = []
        for ln in lines:
            u = _line_uuid(ln)
            if not u:
                continue  # drop unparseable garbage so the spool can't wedge
            if u in acked:
                continue  # successfully forwarded
            remaining.append(ln)
        tmp = spool + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(remaining)
        os.replace(tmp, spool)
    except Exception:
        pass
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return 0


def _line_uuid(line: str) -> str:
    try:
        return json.loads(line).get("event_uuid", "")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# timeline
# --------------------------------------------------------------------------- #
def _fetch_run(host: str, run_id: str) -> dict | None:
    # 1) central API
    try:
        import requests
        resp = requests.get(
            f"{_url()}/runs/{host}/{run_id}", headers=_auth_headers(), timeout=10
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    # 2) local vault (when run on the central host)
    vault_dir = os.environ.get("RUN_LEDGER_VAULT") or os.path.join(_var_dir(), "vault")
    if os.path.isdir(vault_dir):
        try:
            from lib import vault as V
            run = V.Vault(vault_dir).get_run(host, run_id)
            if run:
                return run
        except Exception:
            pass
    # 3) local spool fallback
    events = []
    if os.path.exists(_spool_path()):
        with open(_spool_path(), encoding="utf-8") as fh:
            for ln in fh:
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if ev.get("host") == host and ev.get("run_id") == run_id:
                    events.append(ev)
    if events:
        return {"host": host, "run_id": run_id, "agents": [], "events": events}
    return None


def _vault_line(ev: dict) -> str:
    from lib import vault as V
    return V.timeline_line(ev)


def _dur(t0: str | None, t1: str | None) -> str:
    try:
        a = datetime.strptime(t0, _TS_FMT)
        b = datetime.strptime(t1, _TS_FMT)
        secs = int((b - a).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
    except Exception:
        return "?"


def _plans_and_tools(events: list) -> tuple:
    starts, durations, tools = {}, [], {}
    for ev in events:
        name = (ev.get("event") or "").replace("-", "_")
        if name == "plan_start" and ev.get("plan"):
            starts[ev["plan"]] = ev.get("ts")
        elif name == "plan_finish" and ev.get("plan") in starts:
            durations.append((ev["plan"], starts[ev["plan"]], ev.get("ts"), ev.get("sha")))
        elif name == "tool_use" and ev.get("tool"):
            tools[ev["tool"]] = tools.get(ev["tool"], 0) + 1
    return durations, tools


def _render(run: dict) -> str:
    events = run.get("events", [])
    agents = run.get("agents")
    out = []

    if agents is not None:
        # Per-agent model: one note per agent, linked by run_id.
        agents = sorted(agents, key=lambda a: a.get("started_at") or "")
        starts = [a.get("started_at") for a in agents if a.get("started_at")]
        ends = [a.get("ended_at") for a in agents if a.get("ended_at")]
        ex = next((a for a in agents if a.get("role") == "executor"), None)
        out.append(f"run_id : {run.get('run_id')}")
        out.append(f"host   : {run.get('host')}")
        if ex:
            out.append(f"branch : {ex.get('branch')}")
            out.append(f"status : {ex.get('status')}")
        out.append(f"window : {min(starts) if starts else '?'} -> {max(ends) if ends else '?'}")
        out.append(f"agents : {len(agents)}")
        for a in agents:
            c = a.get("counts") or {}
            out.append(
                f"  - [{a.get('role') or '?'}] {str(a.get('session_id') or '')[:8]}  "
                f"model={a.get('model') or '-'}  tools={c.get('tool_calls', 0)} "
                f"fails={c.get('failures', 0)} events={c.get('events', 0)}  "
                f"{a.get('started_at')} -> {a.get('ended_at')}"
            )
    else:
        fm = run.get("frontmatter", {})
        out.append(f"run_id : {fm.get('run_id')}")
        out.append(f"host   : {fm.get('host')}")
        out.append(f"branch : {fm.get('branch')}")
        out.append(f"status : {fm.get('status')}")
        out.append(f"window : {fm.get('started_at')} -> {fm.get('ended_at')}")
        counts = fm.get("counts") or {}
        if counts:
            out.append("counts : " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    durations, tools = _plans_and_tools(events)
    if durations:
        out.append("plans  :")
        for plan, t0, t1, sha in durations:
            out.append(f"  - {plan}  {_dur(t0, t1)}  sha={sha or '-'}")
    if tools:
        out.append("tools  : " + ", ".join(f"{k}={v}" for k, v in sorted(tools.items())))

    tl = run.get("timeline") or [_vault_line(ev) for ev in events]
    out.append("")
    out.append("timeline:")
    out.extend("  " + ln for ln in tl)
    enr = run.get("enrichment") or []
    if enr:
        out.append("")
        out.append("enrichment:")
        out.extend(f"  - {e.get('file')}" for e in enr)
    return "\n".join(out)


def cmd_rebuild(args) -> int:
    """Regenerate vault notes from their raw event sidecars (server-side).

    Use after a vault-format change so existing notes pick up new frontmatter
    aggregation. Operates directly on RUN_LEDGER_VAULT; run on the central host.
    """
    vault_dir = os.environ.get("RUN_LEDGER_VAULT") or os.path.join(_var_dir(), "vault")
    if not os.path.isdir(vault_dir):
        sys.stderr.write(f"no vault at {vault_dir}\n")
        return 1
    from lib import vault as V
    vault = V.Vault(vault_dir)

    if args.all:
        n = 0
        for fm in vault._agent_frontmatters():
            key, host = fm.get("_key"), fm.get("host")
            if key and host and vault.rebuild(host, key):
                n += 1
        print(json.dumps({"rebuilt": n}))
        return 0
    if args.run:
        host, run_id = (args.run.split("/", 1) if "/" in args.run else (_host(), args.run))
        n = vault.rebuild_run(host, run_id)
        print(json.dumps({"rebuilt": n, "run": f"{host}/{run_id}"}))
        return 0
    sys.stderr.write("usage: run_ledger.py rebuild (--all | --run <host/run_id>)\n")
    return 2


def cmd_timeline(args) -> int:
    ref = args.run or ""
    host, run_id = (ref.split("/", 1) if "/" in ref else (_host(), ref))
    if not run_id:
        sys.stderr.write("usage: run_ledger.py timeline <host/run_id | run_id>\n")
        return 2
    run = _fetch_run(host, run_id)
    if run is None:
        sys.stderr.write(f"no data for {host}/{run_id}\n")
        return 1
    print(_render(run))
    return 0


# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(prog="run_ledger.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record")
    r.add_argument("--source")
    r.add_argument("--event")
    r.add_argument("--run-id", dest="run_id")
    r.add_argument("--field", action="append", default=[])
    r.set_defaults(func=cmd_record)

    ip = sub.add_parser("ingest-pane")
    ip.add_argument("--run-id", dest="run_id", required=True)
    ip.add_argument("--slug", required=True)
    ip.add_argument("--repo", default="")
    ip.set_defaults(func=cmd_ingest_pane)

    f = sub.add_parser("flush")
    f.set_defaults(func=cmd_flush)

    rb = sub.add_parser("rebuild")
    rb.add_argument("--all", action="store_true")
    rb.add_argument("--run", default="")
    rb.set_defaults(func=cmd_rebuild)

    t = sub.add_parser("timeline")
    t.add_argument("run", nargs="?", default="")
    t.set_defaults(func=cmd_timeline)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
