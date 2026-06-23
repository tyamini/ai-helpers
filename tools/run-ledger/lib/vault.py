"""Shared Markdown/Obsidian-vault helpers for the run-ledger.

Both the central server and the client import this module so they produce
identical note formatting. The vault is a plain folder of Markdown notes,
**one per agent** (executor, per-plan agent, ...), linked into a run tree by a
shared ``run_id`` and a ``parent`` wikilink. A logical run is the set of agent
notes sharing a ``run_id`` (browse via the Obsidian graph or a Dataview query
``WHERE run_id = X``).

Layout::

    <vault>/
      agents/<host>/<session_id>.md        # one agent note: frontmatter + its timeline
      enrichment/<host>__<run_id>__<type>.md
      .index/<host>__<key>.seen            # applied event_uuids (dedupe)
      .index/<host>__<key>.events.jsonl    # raw events (forward-compat)
      runs/<host>/<run_id>.md              # legacy one-note-per-run (read-only fallback)

``<key>`` is the event's ``session_id`` (or, for an event lacking one, its
``run_id``). The model is schemaless and append-only: unknown event fields are
preserved as ``key=value`` on the timeline line and verbatim in the sidecar.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import yaml

# Event fields that are structural (they define the line itself) and so are
# not repeated as key=value tail on a timeline line. ``tool_counts`` is an
# aggregate consumed into frontmatter (the human-readable ``tools`` string is
# what shows on the line), so it is omitted from the tail too.
STRUCTURAL_KEYS = {"event_uuid", "ts", "host", "run_id", "source", "event",
                   "tool_counts"}

AGENT_TAG = "orchestration-agent"
TIMELINE_HEADER = "## Event timeline"


def canonical_event(name: str) -> str:
    """Normalise an event name to underscore form (``run-start`` -> ``run_start``)."""
    return (name or "").strip().replace("-", "_")


# --------------------------------------------------------------------------- #
# Note (de)serialisation
# --------------------------------------------------------------------------- #
def parse_note(text: str) -> tuple[dict, str]:
    """Split a note into (frontmatter_dict, body_string)."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm = yaml.safe_load("\n".join(lines[1:i])) or {}
                body = "\n".join(lines[i + 1:]).lstrip("\n")
                return fm, body
    return {}, text


def render_note(fm: dict, body: str) -> str:
    """Render frontmatter + body back into a Markdown note string."""
    front = yaml.safe_dump(
        fm, sort_keys=False, default_flow_style=False, allow_unicode=True
    ).rstrip("\n")
    return f"---\n{front}\n---\n\n{body.rstrip(chr(10))}\n"


def new_agent_frontmatter(host: str, key: str, run_id: str,
                          session_id: str | None, role: str | None,
                          parent: str | None) -> dict:
    return {
        "session_id": session_id,
        "run_id": run_id,
        "role": role or None,
        "parent": f"[[{host}/{parent}]]" if parent else None,
        "host": host,
        "aliases": [f"{host}/{key}"],
        "model": None,
        "branch": None,
        "status": "running",
        "started_at": None,
        "ended_at": None,
        "counts": {"tool_calls": 0, "failures": 0, "compactions": 0, "events": 0},
        "tags": [AGENT_TAG],
    }


def new_body() -> str:
    return TIMELINE_HEADER + "\n"


# --------------------------------------------------------------------------- #
# Timeline line formatting
# --------------------------------------------------------------------------- #
def _fmt_val(v: Any) -> str:
    s = v if isinstance(v, str) else json.dumps(v)
    if any(c.isspace() for c in s) or '"' in s or s == "":
        return json.dumps(s if isinstance(v, str) else v)
    return s


def timeline_line(ev: dict) -> str:
    ts = ev.get("ts", "")
    name = canonical_event(ev.get("event", ""))
    extras = []
    for k in sorted(ev.keys()):
        if k in STRUCTURAL_KEYS:
            continue
        v = ev[k]
        if v is None or v == "":
            continue
        extras.append(f"{k}={_fmt_val(v)}")
    tail = (" " + " ".join(extras)) if extras else ""
    return f"- {ts} `{name}`{tail}"


def append_timeline(body: str, line: str) -> str:
    return body.rstrip("\n") + "\n" + line + "\n"


# --------------------------------------------------------------------------- #
# Per-agent frontmatter refresh
# --------------------------------------------------------------------------- #
def refresh_agent_frontmatter(fm: dict, ev: dict, host: str) -> None:
    name = canonical_event(ev.get("event", ""))
    ts = ev.get("ts")

    counts = fm.setdefault("counts", {"tool_calls": 0, "failures": 0,
                                      "compactions": 0, "events": 0})
    counts["events"] = counts.get("events", 0) + 1

    if not fm.get("started_at"):
        fm["started_at"] = ts

    if ev.get("model") and not fm.get("model"):
        fm["model"] = ev["model"]
    if ev.get("role"):
        fm["role"] = ev["role"]
    if ev.get("branch"):
        fm["branch"] = ev["branch"]
    if ev.get("parent_session_id") and not fm.get("parent"):
        fm["parent"] = f"[[{host}/{ev['parent_session_id']}]]"

    bump = {
        "tool_use": "tool_calls",
        "tool_fail": "failures",
        "compaction": "compactions",
    }.get(name)
    if bump:
        counts[bump] = counts.get(bump, 0) + 1

    # Per-agent tool summary from ingest-pane: a {tool: count} map. Recorded as a
    # `tools` frontmatter breakdown plus the tool_calls total.
    tc = ev.get("tool_counts")
    if isinstance(tc, dict) and tc:
        tools = fm.setdefault("tools", {})
        total = 0
        for k, v in tc.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            tools[k] = tools.get(k, 0) + n
            total += n
        counts["tool_calls"] = counts.get("tool_calls", 0) + total

    if name == "run_complete":
        fm["status"] = "complete"
        fm["ended_at"] = ts
    elif name == "blocked":
        fm["status"] = "blocked"
        fm["ended_at"] = ts
    elif name == "subagent_stop":
        # a per-plan agent's completion (parsed from its pane.log)
        fm["ended_at"] = ts
        if ev.get("status"):
            fm["status"] = ev["status"]
    elif name == "stop" and not fm.get("ended_at"):
        fm["ended_at"] = ts


# --------------------------------------------------------------------------- #
# Atomic file helpers
# --------------------------------------------------------------------------- #
def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _append_line(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def _read_events(path: str) -> list:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
    return out


# --------------------------------------------------------------------------- #
# Vault store (server side)
# --------------------------------------------------------------------------- #
class Vault:
    """Filesystem-backed Markdown vault, one note per agent.

    All writes assume a single writer process (the server is single-threaded),
    so per-note appends are serialised and never race.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    # paths ---------------------------------------------------------------- #
    def _agent_path(self, host: str, key: str) -> str:
        return os.path.join(self.root, "agents", host, f"{key}.md")

    def _legacy_run_path(self, host: str, run_id: str) -> str:
        return os.path.join(self.root, "runs", host, f"{run_id}.md")

    def _seen_path(self, host: str, key: str) -> str:
        return os.path.join(self.root, ".index", f"{host}__{key}.seen")

    def _events_path(self, host: str, key: str) -> str:
        return os.path.join(self.root, ".index", f"{host}__{key}.events.jsonl")

    def _enrichment_path(self, host: str, run_id: str, etype: str) -> str:
        safe = etype.replace("/", "_")
        return os.path.join(self.root, "enrichment", f"{host}__{run_id}__{safe}.md")

    # dedupe --------------------------------------------------------------- #
    def _seen_set(self, host: str, key: str) -> set:
        path = self._seen_path(host, key)
        if not os.path.exists(path):
            return set()
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}

    # ingest --------------------------------------------------------------- #
    def record(self, ev: dict) -> bool:
        """Apply one event to its agent note. Returns True if appended, False if dup."""
        host = ev.get("host")
        run_id = ev.get("run_id")
        uuid_ = ev.get("event_uuid")
        if not host or not run_id or not uuid_:
            raise ValueError("event missing host/run_id/event_uuid")

        sid = ev.get("session_id")
        key = sid or run_id  # events without a session route to a run-keyed note

        if uuid_ in self._seen_set(host, key):
            return False

        note_path = self._agent_path(host, key)
        if os.path.exists(note_path):
            with open(note_path, encoding="utf-8") as fh:
                fm, body = parse_note(fh.read())
        else:
            fm = new_agent_frontmatter(host, key, run_id, sid,
                                       ev.get("role"), ev.get("parent_session_id"))
            body = new_body()

        body = append_timeline(body, timeline_line(ev))
        refresh_agent_frontmatter(fm, ev, host)
        _atomic_write(note_path, render_note(fm, body))
        _append_line(self._events_path(host, key), json.dumps(ev, ensure_ascii=False))
        _append_line(self._seen_path(host, key), uuid_)
        return True

    def rebuild(self, host: str, key: str) -> bool:
        """Regenerate one agent note from its raw events sidecar."""
        epath = self._events_path(host, key)
        events = _read_events(epath)
        if not events:
            return False
        run_id = events[0].get("run_id", "")
        sid = events[0].get("session_id")
        fm = new_agent_frontmatter(host, key, run_id, sid,
                                   events[0].get("role"),
                                   events[0].get("parent_session_id"))
        body = new_body()
        for ev in events:
            body = append_timeline(body, timeline_line(ev))
            refresh_agent_frontmatter(fm, ev, host)
        _atomic_write(self._agent_path(host, key), render_note(fm, body))
        return True

    def rebuild_run(self, host: str, run_id: str) -> int:
        """Rebuild every agent note belonging to a run. Returns the count."""
        n = 0
        for fm in self._agent_frontmatters(host):
            if fm.get("run_id") == run_id and fm.get("_key"):
                if self.rebuild(host, fm["_key"]):
                    n += 1
        return n

    # query ---------------------------------------------------------------- #
    def _agent_frontmatters(self, host: str | None = None) -> list[dict]:
        """All agent-note frontmatters (each annotated with its file `_key`/`host`)."""
        out = []
        base = os.path.join(self.root, "agents")
        if not os.path.isdir(base):
            return out
        hosts = [host] if host else sorted(os.listdir(base))
        for h in hosts:
            hdir = os.path.join(base, h)
            if not os.path.isdir(hdir):
                continue
            for fn in sorted(os.listdir(hdir)):
                if not fn.endswith(".md"):
                    continue
                with open(os.path.join(hdir, fn), encoding="utf-8") as fh:
                    fm, _ = parse_note(fh.read())
                fm["_key"] = fn[:-3]
                fm["host"] = fm.get("host") or h
                out.append(fm)
        return out

    def list_runs(self) -> list[dict]:
        """One summary per run_id, aggregated across its agent notes."""
        runs: dict = {}
        for fm in self._agent_frontmatters():
            rid = fm.get("run_id")
            if not rid:
                continue
            key = (fm.get("host"), rid)
            r = runs.get(key)
            if r is None:
                r = {"run_id": rid, "host": fm.get("host"), "branch": None,
                     "status": "running", "started_at": None, "ended_at": None,
                     "agents": 0, "counts": {"tool_calls": 0, "failures": 0,
                                             "compactions": 0, "events": 0}}
                runs[key] = r
            r["agents"] += 1
            c = fm.get("counts") or {}
            for k in r["counts"]:
                r["counts"][k] += c.get(k, 0)
            st = fm.get("started_at")
            if st and (r["started_at"] is None or st < r["started_at"]):
                r["started_at"] = st
            en = fm.get("ended_at")
            if en and (r["ended_at"] is None or en > r["ended_at"]):
                r["ended_at"] = en
            if fm.get("role") == "executor":
                r["branch"] = fm.get("branch")
                r["status"] = fm.get("status") or r["status"]

        out = list(runs.values())
        # include legacy one-note-per-run summaries (old format)
        legacy_dir = os.path.join(self.root, "runs")
        if os.path.isdir(legacy_dir):
            for h in sorted(os.listdir(legacy_dir)):
                hdir = os.path.join(legacy_dir, h)
                if not os.path.isdir(hdir):
                    continue
                for fn in sorted(os.listdir(hdir)):
                    if fn.endswith(".md"):
                        with open(os.path.join(hdir, fn), encoding="utf-8") as fh:
                            fm, _ = parse_note(fh.read())
                        fm["legacy"] = True
                        out.append(fm)
        return out

    def get_run(self, host: str, run_id: str) -> dict | None:
        """Return the run's agent notes + merged events, or a legacy single note."""
        agents, events = [], []
        for fm in self._agent_frontmatters(host):
            if fm.get("run_id") != run_id:
                continue
            agents.append(fm)
            events.extend(_read_events(self._events_path(host, fm["_key"])))

        if agents:
            events.sort(key=lambda e: e.get("ts") or "")
            return {
                "host": host,
                "run_id": run_id,
                "agents": agents,
                "events": events,
                "enrichment": self._enrichment_for(host, run_id),
            }

        # legacy fallback: old one-note-per-run format
        legacy = self._legacy_run_path(host, run_id)
        if os.path.exists(legacy):
            with open(legacy, encoding="utf-8") as fh:
                fm, body = parse_note(fh.read())
            return {
                "frontmatter": fm,
                "timeline": [ln for ln in body.splitlines() if ln.startswith("- ")],
                "events": _read_events(self._events_path(host, run_id)),
                "enrichment": self._enrichment_for(host, run_id),
            }
        return None

    def _enrichment_for(self, host: str, run_id: str) -> list:
        enrichment = []
        edir = os.path.join(self.root, "enrichment")
        prefix = f"{host}__{run_id}__"
        if os.path.isdir(edir):
            for fn in sorted(os.listdir(edir)):
                if fn.startswith(prefix) and fn.endswith(".md"):
                    with open(os.path.join(edir, fn), encoding="utf-8") as fh:
                        efm, ebody = parse_note(fh.read())
                    enrichment.append({"file": fn, "frontmatter": efm, "body": ebody})
        return enrichment

    # enrichment (link-based, additive) ------------------------------------ #
    def put_enrichment(self, host: str, run_id: str, etype: str, content: str,
                       meta: dict | None = None) -> str:
        link = f"[[{host}/{run_id}]]"
        fm = {"type": etype, "run": link, "host": host, "run_id": run_id}
        if meta:
            fm.update(meta)
        body = f"Enriches {link}\n\n{content.rstrip(chr(10))}\n"
        path = self._enrichment_path(host, run_id, etype)
        _atomic_write(path, render_note(fm, body))
        return path
