"""Shared Markdown/Obsidian-vault helpers for the run-ledger.

Both the central server and the client import this module so they produce
identical note formatting. The vault is a plain folder of Markdown notes,
one per run, that opens directly in Obsidian.

Layout::

    <vault>/
      runs/<host>/<run_id>.md              # frontmatter + append-only timeline
      enrichment/<host>__<run_id>__<type>.md
      .index/<host>__<run_id>.seen         # applied event_uuids (dedupe)
      .index/<host>__<run_id>.events.jsonl # raw events (forward-compat)

The design is schemaless and append-only: unknown event fields are never
lost (they appear as ``key=value`` on the timeline line and verbatim in the
``.events.jsonl`` sidecar), so new fields never require a migration.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import yaml

# Event fields that are structural (they define the line itself) and so are
# not repeated as key=value tail on a timeline line.
STRUCTURAL_KEYS = {"event_uuid", "ts", "host", "run_id", "source", "event"}

RUN_TAG = "orchestration-run"
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


def new_frontmatter(host: str, run_id: str) -> dict:
    return {
        "run_id": run_id,
        "host": host,
        "aliases": [f"{host}/{run_id}"],
        "branch": None,
        "status": "running",
        "started_at": None,
        "ended_at": None,
        "plans": [],
        "sources": [],
        "counts": {
            "subagents": 0,
            "tool_calls": 0,
            "failures": 0,
            "compactions": 0,
            "plans_done": 0,
        },
        "sessions": {},        # session_id -> {role, model, events, tool_calls, failures, first_seen, last_seen}
        "subagent_starts": [],  # raw subagentStart hook records (type/timing); often empty
        "tags": [RUN_TAG],
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
# Frontmatter refresh
# --------------------------------------------------------------------------- #
def refresh_frontmatter(fm: dict, ev: dict) -> None:
    name = canonical_event(ev.get("event", ""))
    ts = ev.get("ts")
    src = ev.get("source")

    fm.setdefault("sources", [])
    if src and src not in fm["sources"]:
        fm["sources"].append(src)

    branch = ev.get("branch")
    if branch:
        fm["branch"] = branch

    fm.setdefault("plans", [])
    plan = ev.get("plan")
    if plan and plan not in fm["plans"]:
        fm["plans"].append(plan)

    if not fm.get("started_at"):
        fm["started_at"] = ts

    counts = fm.setdefault(
        "counts",
        {"subagents": 0, "tool_calls": 0, "failures": 0, "compactions": 0, "plans_done": 0},
    )
    bump = {
        "subagent_start": "subagents",
        "tool_use": "tool_calls",
        "tool_fail": "failures",
        "compaction": "compactions",
        "plan_finish": "plans_done",
    }.get(name)
    if bump:
        counts[bump] = counts.get(bump, 0) + 1

    # Per-session (per-agent) breakdown: conversation_id/session_id is the only
    # reliable per-agent key (subagentStart fires in the parent's session and its
    # subagent_id never matches the child's session id, so sessions are the unit).
    sid = ev.get("session_id")
    if sid:
        sessions = fm.setdefault("sessions", {})
        s = sessions.get(sid)
        if s is None:
            s = {"model": None, "events": 0, "tool_calls": 0, "failures": 0,
                 "first_seen": ts, "last_seen": ts}
            sessions[sid] = s
        s["events"] += 1
        s["last_seen"] = ts
        if ev.get("model") and not s["model"]:
            s["model"] = ev["model"]
        if name == "tool_use":
            s["tool_calls"] += 1
        elif name == "tool_fail":
            s["failures"] += 1
        # Truthful subagent count derived from distinct sessions (earliest =
        # executor, the rest are subagents). Robust even when subagentStart never
        # fires, which is the common case for loop-dispatched subagents.
        counts["subagents"] = max(0, len(sessions) - 1)
        # Label roles by first-seen order: earliest session is the executor.
        for i, (k, v) in enumerate(
            sorted(sessions.items(), key=lambda kv: kv[1].get("first_seen") or "")
        ):
            v["role"] = "executor" if i == 0 else f"subagent-{i}"

    if name == "subagent_start":
        fm.setdefault("subagent_starts", []).append({
            "subagent_type": ev.get("subagent_type"),
            "subagent_id": ev.get("subagent_id"),
            "parent_session_id": ev.get("parent_session_id"),
            "at": ts,
        })

    if name == "run_complete":
        fm["status"] = "complete"
        fm["ended_at"] = ts
    elif name == "blocked":
        fm["status"] = "blocked"
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


# --------------------------------------------------------------------------- #
# Vault store (server side)
# --------------------------------------------------------------------------- #
class Vault:
    """Filesystem-backed Markdown vault. Used by the central server.

    All writes assume a single writer process (the server is single-threaded),
    so per-note appends are serialised and never race.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))

    # paths ---------------------------------------------------------------- #
    def _note_path(self, host: str, run_id: str) -> str:
        return os.path.join(self.root, "runs", host, f"{run_id}.md")

    def _seen_path(self, host: str, run_id: str) -> str:
        return os.path.join(self.root, ".index", f"{host}__{run_id}.seen")

    def _events_path(self, host: str, run_id: str) -> str:
        return os.path.join(self.root, ".index", f"{host}__{run_id}.events.jsonl")

    def _enrichment_path(self, host: str, run_id: str, etype: str) -> str:
        safe = etype.replace("/", "_")
        return os.path.join(self.root, "enrichment", f"{host}__{run_id}__{safe}.md")

    # dedupe --------------------------------------------------------------- #
    def _seen_set(self, host: str, run_id: str) -> set:
        path = self._seen_path(host, run_id)
        if not os.path.exists(path):
            return set()
        with open(path, encoding="utf-8") as fh:
            return {ln.strip() for ln in fh if ln.strip()}

    # ingest --------------------------------------------------------------- #
    def record(self, ev: dict) -> bool:
        """Apply one event. Returns True if appended, False if a duplicate."""
        host = ev.get("host")
        run_id = ev.get("run_id")
        uuid = ev.get("event_uuid")
        if not host or not run_id or not uuid:
            raise ValueError("event missing host/run_id/event_uuid")

        if uuid in self._seen_set(host, run_id):
            return False

        note_path = self._note_path(host, run_id)
        if os.path.exists(note_path):
            with open(note_path, encoding="utf-8") as fh:
                fm, body = parse_note(fh.read())
        else:
            fm, body = new_frontmatter(host, run_id), new_body()

        body = append_timeline(body, timeline_line(ev))
        refresh_frontmatter(fm, ev)
        _atomic_write(note_path, render_note(fm, body))
        _append_line(self._events_path(host, run_id), json.dumps(ev, ensure_ascii=False))
        _append_line(self._seen_path(host, run_id), uuid)
        return True

    def rebuild(self, host: str, run_id: str) -> bool:
        """Regenerate a run note from its raw events sidecar.

        Used to retro-apply note-format/frontmatter changes to existing runs.
        The sidecar (.events.jsonl) is the source of truth; the note is derived.
        """
        epath = self._events_path(host, run_id)
        if not os.path.exists(epath):
            return False
        fm, body = new_frontmatter(host, run_id), new_body()
        with open(epath, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                body = append_timeline(body, timeline_line(ev))
                refresh_frontmatter(fm, ev)
        _atomic_write(self._note_path(host, run_id), render_note(fm, body))
        return True

    # query ---------------------------------------------------------------- #
    def list_runs(self) -> list[dict]:
        runs_dir = os.path.join(self.root, "runs")
        out = []
        if not os.path.isdir(runs_dir):
            return out
        for host in sorted(os.listdir(runs_dir)):
            hdir = os.path.join(runs_dir, host)
            if not os.path.isdir(hdir):
                continue
            for fn in sorted(os.listdir(hdir)):
                if not fn.endswith(".md"):
                    continue
                with open(os.path.join(hdir, fn), encoding="utf-8") as fh:
                    fm, _ = parse_note(fh.read())
                out.append(fm)
        return out

    def get_run(self, host: str, run_id: str) -> dict | None:
        note_path = self._note_path(host, run_id)
        if not os.path.exists(note_path):
            return None
        with open(note_path, encoding="utf-8") as fh:
            fm, body = parse_note(fh.read())

        events = []
        epath = self._events_path(host, run_id)
        if os.path.exists(epath):
            with open(epath, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if ln:
                        try:
                            events.append(json.loads(ln))
                        except json.JSONDecodeError:
                            pass

        enrichment = []
        edir = os.path.join(self.root, "enrichment")
        prefix = f"{host}__{run_id}__"
        if os.path.isdir(edir):
            for fn in sorted(os.listdir(edir)):
                if fn.startswith(prefix) and fn.endswith(".md"):
                    with open(os.path.join(edir, fn), encoding="utf-8") as fh:
                        efm, ebody = parse_note(fh.read())
                    enrichment.append({"file": fn, "frontmatter": efm, "body": ebody})

        return {
            "frontmatter": fm,
            "timeline": [ln for ln in body.splitlines() if ln.startswith("- ")],
            "events": events,
            "enrichment": enrichment,
        }

    # enrichment (link-based, additive) ------------------------------------ #
    def put_enrichment(self, host: str, run_id: str, etype: str, content: str,
                       meta: dict | None = None) -> str:
        link = f"[[{host}/{run_id}]]"
        fm = {
            "type": etype,
            "run": link,
            "host": host,
            "run_id": run_id,
        }
        if meta:
            fm.update(meta)
        body = f"Enriches {link}\n\n{content.rstrip(chr(10))}\n"
        path = self._enrichment_path(host, run_id, etype)
        _atomic_write(path, render_note(fm, body))
        return path
