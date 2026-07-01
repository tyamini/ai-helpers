#!/usr/bin/env python3
"""run-ledger enrichment: distill transcripts, rename notes, write findings.

Deterministic + fail-open (exit 0), like the rest of the client. The LLM-authored
parts (finding/synthesis prose) are written to files by a skill and pushed by the
put-finding/put-synthesis subcommands; distillation, harness inventory, renaming,
and the transcript archive are fully deterministic here.

Commands: prep | rename | put-archive | put-finding | put-synthesis
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys

CLIENT_DIR = os.path.dirname(os.path.abspath(__file__))
TOOL_DIR = os.path.dirname(CLIENT_DIR)
sys.path.insert(0, TOOL_DIR)


def _rl():
    spec = importlib.util.spec_from_file_location(
        "run_ledger", os.path.join(CLIENT_DIR, "run_ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


RL = _rl()

MAX_RESULT_CHARS = 400        # per tool-result summary
MAX_MSG_CHARS = 4000          # per assistant message on the archive


def _text_blocks(msg) -> str:
    """Join the text blocks of a user/assistant message payload."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                parts.append(str(b["text"]))
    return "\n".join(parts)


def _truncate(s: str, limit: int) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    if len(s) <= limit:
        return s
    keep = limit // 2
    return f"{s[:keep]}\n…[truncated {len(s) - limit} chars]…\n{s[-keep:]}"


def _result_summary(payload: dict) -> str:
    """Compact one completed tool_call's result; drop blob refs and giant text."""
    inner = next((v for k, v in payload.items() if k.endswith("ToolCall")), None)
    res = inner.get("result") if isinstance(inner, dict) else None
    if res is None:
        return ""
    flat = json.dumps(res, ensure_ascii=False)
    # drop the file-content blob id noise
    flat = flat.replace('"contentBlobId"', '"_blob"')
    return _truncate(flat, MAX_RESULT_CHARS)


def _short_args(payload: dict) -> str:
    inner = next((v for k, v in payload.items() if k.endswith("ToolCall")), {})
    args = inner.get("args") if isinstance(inner, dict) else {}
    if not isinstance(args, dict):
        return ""
    for k in ("command", "path", "query", "pattern", "prompt", "file_path"):
        if args.get(k):
            return _truncate(str(args[k]), 160)
    return _truncate(json.dumps(args, ensure_ascii=False), 160)


_HARNESS_TOOLS = ("dbuild", "dtest", "make", "pytest", "cmake", "ninja")


def _classify_read(path, harness) -> None:
    """Classify a Read path as skill / rule for the harness inventory.

    Skills are only counted from ``SKILL.md`` reads (by skill name); a script that
    merely lives under a ``/skills/`` dir is a script, not a skill, and is captured
    from its shell invocation instead.
    """
    p = path or ""
    base = os.path.basename(p)
    if base == "SKILL.md":
        harness["skills"].add(os.path.basename(os.path.dirname(p)) or p)
    elif p.endswith(".mdc") or "/rules/" in p or base in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        harness["rules"].add(base or p)


def _scripts_from_cmd(cmd, harness) -> None:
    """Record scripts/harness tools invoked by a Shell command."""
    c = cmd or ""
    for m in re.findall(r"[\w./-]+\.(?:sh|py)\b", c):
        harness["scripts"].add(os.path.basename(m))
    for tok in _HARNESS_TOOLS:
        if re.search(rf"\b{tok}\b", c):
            harness["scripts"].add(tok)


def distill_stream(lines) -> dict:
    """Turn pane.log stream-json lines into a distilled transcript + extracts.

    Returns {transcript, prompt, directives, final_summary, errors, harness}.
    Keeps assistant text + tool calls/results; drops thinking, token/usage blobs,
    monitor notifications, and partial-output duplicates (only 'completed' calls).
    """
    out, directives, errors = [], [], []
    harness = {"skills": set(), "rules": set(), "scripts": set()}
    prompt = None
    last_assistant = ""
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        t = o.get("type")
        ts = RL._ts_from_ms(o["timestamp_ms"]) if isinstance(o.get("timestamp_ms"), (int, float)) else ""
        if t == "system" and o.get("subtype") == "init":
            out.append(f"- {ts} `init` model={o.get('model')}")
        elif t == "user":
            txt = _text_blocks(o.get("message"))
            if txt and prompt is None:
                prompt = txt                       # first user msg = dispatch prompt
            elif txt:
                directives.append(txt)             # later = injected directives
                out.append(f"- {ts} DIRECTIVE: {_truncate(txt, 400)}")
        elif t == "assistant":
            txt = _text_blocks(o.get("message"))
            if txt and txt != last_assistant:      # drop streamed duplicate re-emits
                out.append(f"- {ts} 💬 {_truncate(txt, MAX_MSG_CHARS)}")
            if txt:
                last_assistant = txt
        elif t == "tool_call" and o.get("subtype") == "completed":
            payload = o.get("tool_call") or {}
            label = RL._tool_label(payload) or "?"
            inner = next((v for k, v in payload.items() if k.endswith("ToolCall")), {})
            cargs = inner.get("args") if isinstance(inner, dict) else {}
            if isinstance(cargs, dict):
                if label == "Read":
                    _classify_read(cargs.get("path"), harness)
                elif label == "Shell":
                    _scripts_from_cmd(cargs.get("command"), harness)
            summ = _result_summary(payload)
            if '"is_error": true' in summ.lower() or '"success": false' in summ.lower():
                errors.append(f"{label}: {summ}")
            out.append(f"- {ts} {label}({_short_args(payload)}) → {summ}")
        elif t == "result" and o.get("is_error"):
            errors.append(_truncate(str(o.get("result", "")), 400))
        # thinking / task_notification / started calls: dropped
    return {
        "transcript": "\n".join(out),
        "prompt": prompt or "",
        "directives": directives,
        "final_summary": last_assistant,
        "errors": errors,
        "harness": {k: sorted(v) for k, v in harness.items()},
    }


# --------------------------------------------------------------------------- #
# manifest + prep
# --------------------------------------------------------------------------- #
def _enrich_dir(run_id: str) -> str:
    return os.path.join(RL._exec_run_dir(run_id), "enrich")


def _read_meta(run_id: str) -> dict:
    try:
        with open(os.path.join(RL._exec_run_dir(run_id), "meta.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def build_manifest(run_id: str) -> dict:
    """Deterministic map of run notes → meaningful names + per-agent extracts."""
    host = RL._host()
    meta = _read_meta(run_id)
    agents = [{"role": "executor", "slug": None, "old_key": run_id,
               "new_name": f"{run_id}__executor",
               "vault_note": f"{host}/{run_id}__executor"}]
    for slug in meta.get("plans", []):
        plan_dir = os.path.join(RL._exec_run_dir(run_id), "plans", slug)
        if not os.path.isdir(plan_dir):
            continue
        verdict = RL._read_verdict(plan_dir)
        parsed = RL._parse_pane(os.path.join(plan_dir, "pane.log"))
        sid = parsed.get("session_id") or verdict.get("chat_id")
        if not sid:
            continue
        agents.append({
            "role": "subagent", "slug": slug, "old_key": sid,
            "new_name": f"{run_id}__{slug}__subagent",
            "vault_note": f"{host}/{run_id}__{slug}__subagent",
            "model": parsed.get("model"), "verdict": verdict,
            "tool_counts": parsed.get("tool_counts", {}),
            "started_at": parsed.get("started_at"),
            "ended_at": parsed.get("ended_at"),
        })
    return {"run_id": run_id, "host": host, "agents": agents}


def cmd_prep(args) -> int:
    """Distill each plan's pane.log + write manifest.json. Fail-open."""
    try:
        run_id = args.run_id
        man = build_manifest(run_id)
        out_dir = _enrich_dir(run_id)
        os.makedirs(out_dir, exist_ok=True)
        for a in man["agents"]:
            slug = a.get("slug")
            if not slug:
                continue
            pane = os.path.join(RL._exec_run_dir(run_id), "plans", slug, "pane.log")
            lines = []
            if os.path.exists(pane):
                with open(pane, errors="replace") as fh:
                    lines = fh.readlines()
            d = distill_stream(lines)
            a["prompt"] = d["prompt"]
            a["directives"] = d["directives"]
            a["final_summary"] = d["final_summary"]
            a["errors"] = d["errors"]
            a["harness"] = d["harness"]
            with open(os.path.join(out_dir, f"{slug}.distilled.md"), "w") as fh:
                fh.write(d["transcript"] + "\n")
        agg = {"skills": set(), "rules": set(), "scripts": set()}
        for a in man["agents"]:
            for k, vals in (a.get("harness") or {}).items():
                agg[k].update(vals)
        man["harness"] = {k: sorted(v) for k, v in agg.items()}
        with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
            json.dump(man, fh, indent=2)
        print(json.dumps({"prepared": len(man["agents"]), "dir": out_dir}))
    except Exception as exc:
        sys.stderr.write(f"prep: {exc}\n")
    return 0


# --------------------------------------------------------------------------- #
# HTTP helpers (reuse run_ledger transport)
# --------------------------------------------------------------------------- #
def _post(path: str, body: dict) -> dict:
    try:
        import requests
        resp = requests.post(RL._url() + path, json=body,
                             headers=RL._auth_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        sys.stderr.write(f"POST {path}: {exc}\n")
        return {}


def _put_enrichment(host: str, run_id: str, etype: str, content: str,
                    meta: dict | None = None) -> dict:
    try:
        import requests
        resp = requests.put(
            f"{RL._url()}/runs/{host}/{run_id}/enrichment/{etype}",
            json={"content": content, "meta": meta or {}},
            headers=RL._auth_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        sys.stderr.write(f"PUT enrichment/{etype}: {exc}\n")
        return {}


def _manifest(run_id: str) -> dict:
    try:
        with open(os.path.join(_enrich_dir(run_id), "manifest.json")) as fh:
            return json.load(fh)
    except Exception:
        return build_manifest(run_id)


# --------------------------------------------------------------------------- #
# commands: rename / put-archive / put-finding / put-synthesis
# --------------------------------------------------------------------------- #
def cmd_rename(args) -> int:
    try:
        man = _manifest(args.run_id)
        renames = [{"old_key": a["old_key"], "new_name": a["new_name"]}
                   for a in man["agents"]]
        r = _post(f"/runs/{man['host']}/{args.run_id}/rename", {"renames": renames})
        print(json.dumps(r))
    except Exception as exc:
        sys.stderr.write(f"rename: {exc}\n")
    return 0


def cmd_put_archive(args) -> int:
    try:
        man = _manifest(args.run_id)
        host = man["host"]
        for a in man["agents"]:
            slug = a.get("slug")
            if not slug:
                continue
            path = os.path.join(_enrich_dir(args.run_id), f"{slug}.distilled.md")
            transcript = open(path).read() if os.path.exists(path) else ""
            quoted = "\n".join("> " + ln for ln in transcript.splitlines())
            content = (f"Related agent: [[{a['vault_note']}]]\n\n"
                       f"> [!note]- Distilled transcript ({slug})\n{quoted}\n")
            _put_enrichment(host, args.run_id, f"transcript-{slug}", content,
                            {"agent": a["vault_note"], "kind": "transcript"})
        print(json.dumps({"archived": sum(1 for a in man['agents'] if a.get('slug'))}))
    except Exception as exc:
        sys.stderr.write(f"put-archive: {exc}\n")
    return 0


def cmd_put_finding(args) -> int:
    try:
        man = _manifest(args.run_id)
        agent = next((a for a in man["agents"] if a.get("slug") == args.slug), None)
        note = agent["vault_note"] if agent else ""
        content = open(args.file).read()
        body = f"Related agent: [[{note}]]\n\n{content}"
        _put_enrichment(man["host"], args.run_id, f"agent-{args.slug}", body,
                        {"agent": note, "kind": "finding"})
        print(json.dumps({"finding": args.slug}))
    except Exception as exc:
        sys.stderr.write(f"put-finding: {exc}\n")
    return 0


def cmd_put_synthesis(args) -> int:
    try:
        man = _manifest(args.run_id)
        host = man["host"]
        links = " ".join(f"[[{a['vault_note']}]]" for a in man["agents"])
        content = open(args.file).read()
        body = f"Agents: {links}\n\n{content}"
        _put_enrichment(host, args.run_id, "synthesis", body, {"kind": "synthesis"})
        print(json.dumps({"synthesis": args.run_id}))
    except Exception as exc:
        sys.stderr.write(f"put-synthesis: {exc}\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="enrich.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("prep")
    pp.add_argument("--run-id", dest="run_id", required=True)
    pp.set_defaults(func=cmd_prep)

    rn = sub.add_parser("rename")
    rn.add_argument("--run-id", dest="run_id", required=True)
    rn.set_defaults(func=cmd_rename)

    pa = sub.add_parser("put-archive")
    pa.add_argument("--run-id", dest="run_id", required=True)
    pa.set_defaults(func=cmd_put_archive)

    pf = sub.add_parser("put-finding")
    pf.add_argument("--run-id", dest="run_id", required=True)
    pf.add_argument("--slug", required=True)
    pf.add_argument("--file", required=True)
    pf.set_defaults(func=cmd_put_finding)

    ps = sub.add_parser("put-synthesis")
    ps.add_argument("--run-id", dest="run_id", required=True)
    ps.add_argument("--file", required=True)
    ps.set_defaults(func=cmd_put_synthesis)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
