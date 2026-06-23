#!/usr/bin/env python3
"""run-ledger client: record / hook / flush / timeline / init / resolve.

Runs on every machine. ``record`` and ``hook`` are the hot path and are
**fail-open**: they never raise and always exit 0, so a telemetry problem can
never block an agent.

Scope (the "no unrelated entries" guarantee), decentralized via a live registry
-------------------------------------------------------------------------------
Each tracked agent registers itself at skill start by running
``run_ledger.py init --run-id X --role R [--parent P]``. That command is an
ordinary shell tool call; the ``postToolUse`` hook that fires for it carries the
agent's ``session_id`` (which the agent itself does not know), so the recorder
writes a machine-local registry entry ``var/live/<session_id>.json``.

From then on, a hook event is **kept only if its session is registered**
(``var/live/<session_id>.json`` exists); its ``run_id``/``role``/``parent`` are
stamped from the registry. Unregistered sessions (unrelated/ad-hoc work) are
dropped. Because the key is the globally-unique ``session_id``, any number of
runs can be live on one machine at once without cross-attribution.

``--source notify`` events are emitted by the executor itself with an explicit
``--run-id`` (and ``--field session_id=<executor>``), so they are always in
scope.

Commands
--------
- init     marker the hook observes to register this session; ``--end`` deregisters a run
- resolve  print the session_id registered for a given run_id+role (executor self-id)
- record   build an event, append to the local spool, kick a flush
- hook      read a Cursor hook payload on stdin, register/scope, record
- flush    forward spooled events to the central service; drop acked, keep rest
- timeline render a deterministic per-run summary (central API, vault, or spool)

Config (env)
------------
- RUN_LEDGER_URL      central base URL (default http://tyamini-dev:8723)
- RUN_LEDGER_TOKEN    bearer token (default empty -> no auth header)
- RUN_LEDGER_VAR      var dir (default <tool>/var)
- RUN_LEDGER_VAULT    server vault dir, for local timeline reads on tyamini-dev
- RUN_LEDGER_MAX_AGE  registry-entry staleness TTL seconds (default 86400)
- RUN_LEDGER_HOOK_DEBUG  when set, dump raw hook payloads to var/hook-raw.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
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


def _live_dir() -> str:
    return os.path.join(_var_dir(), "live")


def _live_path(session_id: str) -> str:
    return os.path.join(_live_dir(), session_id + ".json")


def _lock_path() -> str:
    return os.path.join(_var_dir(), "flush.lock")


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def _host() -> str:
    return socket.gethostname()


def _url() -> str:
    return (os.environ.get("RUN_LEDGER_URL") or DEFAULT_URL).rstrip("/")


def _max_age() -> int:
    try:
        return int(os.environ.get("RUN_LEDGER_MAX_AGE", "86400"))
    except ValueError:
        return 86400


def _auth_headers() -> dict:
    token = os.environ.get("RUN_LEDGER_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


# --------------------------------------------------------------------------- #
# Live-session registry (machine-local; the decentralized scope filter)
# --------------------------------------------------------------------------- #
def _register(session_id: str, run_id: str, role: str, parent: str) -> None:
    if not session_id or not run_id:
        return
    os.makedirs(_live_dir(), exist_ok=True)
    data = {
        "session_id": session_id,
        "run_id": run_id,
        "role": role or "",
        "parent_session_id": parent or "",
        "registered_at": _now(),
    }
    tmp = _live_path(session_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _live_path(session_id))


def _lookup(session_id: str) -> dict | None:
    if not session_id:
        return None
    try:
        with open(_live_path(session_id), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _scan_live(run_id: str, role: str | None = None) -> dict | None:
    d = _live_dir()
    if not os.path.isdir(d):
        return None
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                e = json.load(fh)
        except Exception:
            continue
        if e.get("run_id") == run_id and (role is None or e.get("role") == role):
            return e
    return None


def _end_run(run_id: str) -> None:
    d = _live_dir()
    if not run_id or not os.path.isdir(d):
        return
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, encoding="utf-8") as fh:
                e = json.load(fh)
            if e.get("run_id") == run_id:
                os.remove(p)
        except Exception:
            pass


def _prune() -> None:
    """Drop registry entries older than the TTL (crashed runs that never ended)."""
    d = _live_dir()
    if not os.path.isdir(d):
        return
    cutoff = _max_age()
    now = datetime.now(timezone.utc)
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            with open(p, encoding="utf-8") as fh:
                e = json.load(fh)
            t = datetime.strptime(e.get("registered_at", ""), _TS_FMT).replace(tzinfo=timezone.utc)
            if (now - t).total_seconds() > cutoff:
                os.remove(p)
        except Exception:
            pass


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
        pass  # flush is best-effort; the stop hook / next record will retry


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

    Scoping already happened upstream (registry lookup in cmd_hook, or an
    explicit run_id for notify), so this just needs a run_id to attribute to.
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
# init / resolve (live-session registration)
# --------------------------------------------------------------------------- #
def cmd_init(args) -> int:
    """Marker the hook observes to register this session under a run.

    The actual registration is written by the hook (it alone knows the
    session_id). `--end` deregisters the whole run now, which is deterministic
    and needs no session_id.
    """
    try:
        _prune()
        if args.end:
            _end_run(args.run_id)
            print(json.dumps({"ended": args.run_id}))
        else:
            print(json.dumps({"init": args.run_id, "role": args.role,
                              "note": "registration happens via the postToolUse hook"}))
    except Exception:
        pass
    return 0


def cmd_resolve(args) -> int:
    """Print the session_id registered for run_id+role (executor self-identify).

    Retries briefly because the registering hook may fire slightly after the
    `init` tool call returns.
    """
    for _ in range(8):
        ent = _scan_live(args.run_id, args.role)
        if ent and ent.get("session_id"):
            print(ent["session_id"])
            return 0
        time.sleep(0.5)
    sys.stderr.write(f"no registered session for run={args.run_id} role={args.role}\n")
    return 1


# --------------------------------------------------------------------------- #
# hook (reads a Cursor hook payload on stdin)
# --------------------------------------------------------------------------- #
# Cursor hook event -> ledger event name.
_HOOK_EVENT_MAP = {
    "sessionStart": "session_start",
    "subagentStart": "subagent_start",
    "subagentStop": "subagent_stop",
    "postToolUse": "tool_use",
    "postToolUseFailure": "tool_fail",
    "preCompact": "compaction",
    "stop": "stop",
}


def _first(payload: dict, *keys):
    """Return the first present, non-empty value among key variants."""
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    return None


def _parse_init_command(command: str) -> dict | None:
    """If a shell command is a `run_ledger.py init ...`, parse its args."""
    if not command:
        return None
    try:
        toks = shlex.split(command)
    except Exception:
        return None
    if "init" not in toks or not any("run_ledger" in t for t in toks):
        return None

    def val(flag):
        if flag in toks:
            i = toks.index(flag)
            if i + 1 < len(toks):
                return toks[i + 1]
        return None

    return {
        "run_id": val("--run-id"),
        "role": val("--role"),
        "parent": val("--parent"),
        "end": "--end" in toks,
    }


def cmd_hook(args) -> int:
    """Read a hook payload on stdin, register/scope via the live registry, record.

    Always exits 0. Cursor 3.8.x payloads carry conversation_id/session_id and
    model; subagentStart/Stop add subagent_type/subagent_id/parent_conversation_id.
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    cursor_event = args.cursor_event or payload.get("hook_event_name") or ""

    if os.environ.get("RUN_LEDGER_HOOK_DEBUG"):
        try:
            os.makedirs(_var_dir(), exist_ok=True)
            with open(os.path.join(_var_dir(), "hook-raw.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"cursor_event": cursor_event, "payload": payload}) + "\n")
        except Exception:
            pass

    session_id = _first(payload, "session_id", "conversation_id")

    # Registration: observe an `init` tool call and (de)register the session.
    if cursor_event == "postToolUse":
        tin = payload.get("tool_input")
        command = tin.get("command") if isinstance(tin, dict) else None
        init = _parse_init_command(command) if command else None
        if init:
            if init["end"] and init["run_id"]:
                _end_run(init["run_id"])
            elif session_id and init["run_id"]:
                _register(session_id, init["run_id"], init["role"], init["parent"])

    name = _HOOK_EVENT_MAP.get(cursor_event)
    if name and session_id:
        ent = _lookup(session_id)
        if ent:  # registered -> in scope; everything else is dropped
            fields = {"session_id": session_id}
            model = _first(payload, "model", "subagent_model")
            candidates = {
                "model": model,
                "tool": _first(payload, "tool_name", "tool"),
                "subagent_type": payload.get("subagent_type"),
                "subagent_id": payload.get("subagent_id"),
                "status": payload.get("status"),
                "duration_ms": payload.get("duration_ms"),
            }
            for k, v in candidates.items():
                if v not in (None, ""):
                    fields[k] = v
            if ent.get("role"):
                fields["role"] = ent["role"]
            # Parent: the registry (init --parent) is authoritative; fall back to
            # the payload's parent_conversation_id when present.
            parent = ent.get("parent_session_id") or payload.get("parent_conversation_id")
            if parent:
                fields["parent_session_id"] = parent
            _record_event("hook", name, ent["run_id"], fields)

    if cursor_event == "stop":
        _spawn_flush()

    # Observe-only hook: emit empty JSON and succeed (never block the agent).
    sys.stdout.write("{}")
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

    i = sub.add_parser("init")
    i.add_argument("--run-id", dest="run_id", default="")
    i.add_argument("--role", default="")
    i.add_argument("--parent", default="")
    i.add_argument("--end", action="store_true")
    i.set_defaults(func=cmd_init)

    rs = sub.add_parser("resolve")
    rs.add_argument("--run-id", dest="run_id", required=True)
    rs.add_argument("--role", default="executor")
    rs.set_defaults(func=cmd_resolve)

    r = sub.add_parser("record")
    r.add_argument("--source")
    r.add_argument("--event")
    r.add_argument("--run-id", dest="run_id")
    r.add_argument("--field", action="append", default=[])
    r.set_defaults(func=cmd_record)

    h = sub.add_parser("hook")
    h.add_argument("--cursor-event", dest="cursor_event", default="")
    h.set_defaults(func=cmd_hook)

    f = sub.add_parser("flush")
    f.set_defaults(func=cmd_flush)

    t = sub.add_parser("timeline")
    t.add_argument("run", nargs="?", default="")
    t.set_defaults(func=cmd_timeline)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
