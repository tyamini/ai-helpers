#!/usr/bin/env python3
"""run-ledger client: record / flush / timeline.

Runs on every machine. ``record`` is the only command on the hot path and is
**fail-open**: it never raises and always exits 0, so a telemetry problem can
never block an orchestration loop.

Commands
--------
- record   build an event, decide scope, append to the local spool, kick a flush
- flush    forward spooled events to the central service; drop acked, keep rest
- timeline render a deterministic per-run summary (central API, vault, or spool)

Scope (the "no unrelated entries" guarantee)
-------------------------------------------
- ``--source notify`` events always carry a run_id and only fire inside loops.
- ``--source hook`` events are recorded only while a live run is active:
  ``active.json`` exists, is not ended, and is younger than RUN_LEDGER_MAX_AGE.
  If lineage ids are present on both the event and ``active.json`` they must
  match (excludes a concurrent unrelated session). Otherwise the event is
  dropped (exit 0).

Config (env)
------------
- RUN_LEDGER_URL      central base URL (default http://tyamini-dev:8723)
- RUN_LEDGER_TOKEN    bearer token (default empty -> no auth header)
- RUN_LEDGER_VAR      var dir (default <tool>/var)
- RUN_LEDGER_VAULT    server vault dir, for local timeline reads on tyamini-dev
- RUN_LEDGER_MAX_AGE  active-run staleness TTL seconds (default 86400)
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


def _var_dir() -> str:
    return os.environ.get("RUN_LEDGER_VAR") or os.path.join(TOOL_DIR, "var")


def _spool_path() -> str:
    return os.path.join(_var_dir(), "spool.jsonl")


def _active_path() -> str:
    return os.path.join(_var_dir(), "active.json")


def _lock_path() -> str:
    return os.path.join(_var_dir(), "flush.lock")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
# active.json (run lifecycle + scoping anchor)
# --------------------------------------------------------------------------- #
def _read_active() -> dict | None:
    try:
        with open(_active_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_active(data: dict) -> None:
    os.makedirs(_var_dir(), exist_ok=True)
    tmp = _active_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _active_path())


def _active_is_live(active: dict | None) -> bool:
    if not active or active.get("ended"):
        return False
    started = active.get("started_at")
    if not started:
        return False
    try:
        t = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - t).total_seconds()
    return age <= _max_age()


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
    """Core record logic shared by `record` and `hook`. Always returns 0."""
    try:
        name = (name or "").replace("-", "_")
        source = source or "hook"

        run_id = run_id or fields.get("run_id") or ""
        active = _read_active()
        if not run_id and active:
            run_id = active.get("run_id", "")

        # run_start anchors the run: write active.json, always record.
        if name == "run_start":
            _write_active({
                "run_id": run_id,
                "host": _host(),
                "branch": fields.get("branch"),
                "started_at": _now(),
                "root_agent_id": fields.get("root_agent_id"),
                "ended": False,
                "ended_at": None,
            })
        else:
            # Scope decision.
            if source == "hook":
                if not _active_is_live(active):
                    return 0  # no live run -> drop ad-hoc/unrelated activity
                # Lineage scoping when both sides expose a root id.
                ev_root = fields.get("root_agent_id")
                ac_root = active.get("root_agent_id") if active else None
                if ev_root and ac_root and ev_root != ac_root:
                    return 0  # concurrent unrelated session
                if not run_id:
                    run_id = active.get("run_id", "") if active else ""
            # notify (and any non-hook) events always carry their own run_id.

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

        # End the run window so later hook activity is dropped.
        if name in ("run_complete", "blocked") and active:
            active["ended"] = True
            active["ended_at"] = _now()
            try:
                _write_active(active)
            except Exception:
                pass

        _spawn_flush()
    except Exception:
        pass  # fail-open: telemetry must never break the caller
    return 0


def cmd_record(args) -> int:
    fields = _parse_fields(args.field)
    name = args.event or fields.pop("event", "")
    source = args.source or fields.pop("source", "hook")
    run_id = args.run_id or fields.get("run_id") or ""
    return _record_event(source, name, run_id, fields)


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
    """Return the first present, non-empty value among snake/camel key variants."""
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    return None


def cmd_hook(args) -> int:
    """Read a hook payload on stdin, extract fields, record. Always exits 0.

    The exact hook payload schema is not fully documented (KNOWN UNKNOWN: whether
    a parent/root agent id is present). This reads many key variants defensively
    and, when RUN_LEDGER_HOOK_DEBUG=1, dumps raw payloads so the schema can be
    confirmed (the first implementation step in the plan).
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

    name = _HOOK_EVENT_MAP.get(cursor_event)
    if name:
        fields = {}
        st = _first(payload, "subagent_type", "subagentType", "agent_type", "agentType")
        tool = _first(payload, "tool_name", "toolName", "tool", "tool_type", "toolType")
        agent_id = _first(payload, "agent_id", "agentId")
        parent = _first(payload, "parent_agent_id", "parentAgentId", "parent_id", "parentId")
        root = _first(payload, "root_agent_id", "rootAgentId")
        session_id = _first(payload, "session_id", "sessionId", "conversation_id", "conversationId")
        subagent_id = _first(payload, "subagent_id", "subagentId")
        for k, v in (("subagent_type", st), ("tool", tool), ("agent_id", agent_id),
                     ("parent_agent_id", parent), ("root_agent_id", root),
                     ("session_id", session_id), ("subagent_id", subagent_id)):
            if v not in (None, ""):
                fields[k] = v
        _record_event("hook", name, "", fields)

    # The stop/sessionEnd events flush the spool even if nothing was recorded.
    if cursor_event in ("stop", "sessionEnd"):
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
        return {"frontmatter": {"run_id": run_id, "host": host, "status": "?(spool)"},
                "events": events, "timeline": [], "enrichment": []}
    return None


def _render(run: dict) -> str:
    fm = run.get("frontmatter", {})
    events = run.get("events", [])
    out = []
    out.append(f"run_id : {fm.get('run_id')}")
    out.append(f"host   : {fm.get('host')}")
    out.append(f"branch : {fm.get('branch')}")
    out.append(f"status : {fm.get('status')}")
    out.append(f"window : {fm.get('started_at')} -> {fm.get('ended_at')}")
    counts = fm.get("counts") or {}
    if counts:
        out.append("counts : " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    # per-plan durations + tool tally from structured events
    starts, durations, tools = {}, [], {}
    for ev in events:
        name = (ev.get("event") or "").replace("-", "_")
        if name == "plan_start" and ev.get("plan"):
            starts[ev["plan"]] = ev.get("ts")
        elif name == "plan_finish" and ev.get("plan") in starts:
            durations.append((ev["plan"], starts[ev["plan"]], ev.get("ts"), ev.get("sha")))
        elif name == "tool_use" and ev.get("tool"):
            tools[ev["tool"]] = tools.get(ev["tool"], 0) + 1
    if durations:
        out.append("plans  :")
        for plan, t0, t1, sha in durations:
            out.append(f"  - {plan}  {_dur(t0, t1)}  sha={sha or '-'}")
    if tools:
        out.append("tools  : " + ", ".join(f"{k}={v}" for k, v in sorted(tools.items())))

    tl = run.get("timeline") or [V_line(ev) for ev in events]
    out.append("")
    out.append("timeline:")
    out.extend("  " + ln for ln in tl)
    enr = run.get("enrichment") or []
    if enr:
        out.append("")
        out.append("enrichment:")
        out.extend(f"  - {e.get('file')}" for e in enr)
    return "\n".join(out)


def V_line(ev: dict) -> str:
    from lib import vault as V
    return V.timeline_line(ev)


def _dur(t0: str | None, t1: str | None) -> str:
    try:
        a = datetime.strptime(t0, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(t1, "%Y-%m-%dT%H:%M:%SZ")
        secs = int((b - a).total_seconds())
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
    except Exception:
        return "?"


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
