#!/usr/bin/env python3
"""Central run-ledger ingest+query service (tyamini-dev).

A dependency-free stdlib HTTP service that writes a Markdown/Obsidian vault,
one note per agent (linked into a run tree by run_id). Runs single-threaded so
per-note appends are serialised (no file-write races).

Endpoints
---------
- POST /events                                   ingest one event or a batch
- GET  /runs                                     list runs (frontmatter)
- GET  /runs/{host}/{run_id}                     one run: note + events + enrichment
- PUT  /runs/{host}/{run_id}/enrichment/{type}   create/replace a linked enrichment note
- GET  /health                                   liveness

Config (env)
------------
- RUN_LEDGER_VAULT   vault dir (default <tool>/var/vault)
- RUN_LEDGER_VAR     base var dir (default <tool>/var)
- RUN_LEDGER_TOKEN   bearer token; if unset, auth is disabled (dev only)
- RUN_LEDGER_PORT    listen port (default 8723)
- RUN_LEDGER_BIND    bind address (default 0.0.0.0)
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)
from lib import vault as V  # noqa: E402


def _var_dir() -> str:
    return os.environ.get("RUN_LEDGER_VAR") or os.path.join(TOOL_DIR, "var")


def _vault_dir() -> str:
    return os.environ.get("RUN_LEDGER_VAULT") or os.path.join(_var_dir(), "vault")


TOKEN = os.environ.get("RUN_LEDGER_TOKEN", "")
VAULT = V.Vault(_vault_dir())


class Handler(BaseHTTPRequestHandler):
    server_version = "run-ledger/1.0"

    # -- helpers --------------------------------------------------------- #
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        hdr = self.headers.get("Authorization", "")
        return hdr == f"Bearer {TOKEN}"

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return None
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _parts(self):
        path = self.path.split("?", 1)[0].strip("/")
        return path.split("/") if path else []

    def log_message(self, fmt, *args):  # quieter logs
        sys.stderr.write("[run-ledger] " + (fmt % args) + "\n")

    # -- routes ---------------------------------------------------------- #
    def do_GET(self):
        parts = self._parts()
        if parts == ["health"]:
            return self._send(200, {"status": "ok"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            if parts == ["runs"]:
                return self._send(200, {"runs": VAULT.list_runs()})
            if len(parts) == 3 and parts[0] == "runs":
                run = VAULT.get_run(parts[1], parts[2])
                if run is None:
                    return self._send(404, {"error": "not found"})
                return self._send(200, run)
        except Exception as exc:  # never crash the server
            return self._send(500, {"error": str(exc)})
        return self._send(404, {"error": "no route"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self._parts() != ["events"]:
            return self._send(404, {"error": "no route"})
        try:
            payload = self._read_json()
        except Exception as exc:
            return self._send(400, {"error": f"bad json: {exc}"})

        if isinstance(payload, dict) and "events" in payload:
            events = payload["events"]
        elif isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            events = [payload]
        else:
            return self._send(400, {"error": "expected event object or list"})

        acked, errors = [], []
        for ev in events:
            uuid = ev.get("event_uuid") if isinstance(ev, dict) else None
            try:
                VAULT.record(ev)
                # ack on apply OR duplicate: both mean "safe to drop from spool"
                if uuid:
                    acked.append(uuid)
            except Exception as exc:
                errors.append({"event_uuid": uuid, "error": str(exc)})
                # Ack a malformed-but-identifiable event so a poison pill cannot
                # retry forever in the client spool.
                if uuid:
                    acked.append(uuid)
        return self._send(200, {"acked": acked, "errors": errors})

    def do_PUT(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        parts = self._parts()
        # runs/{host}/{run_id}/enrichment/{type}
        if len(parts) == 5 and parts[0] == "runs" and parts[3] == "enrichment":
            try:
                payload = self._read_json() or {}
                content = payload.get("content", "")
                meta = payload.get("meta") or {}
                path = VAULT.put_enrichment(parts[1], parts[2], parts[4], content, meta)
                return self._send(200, {"written": os.path.basename(path)})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
        return self._send(404, {"error": "no route"})


def main() -> int:
    port = int(os.environ.get("RUN_LEDGER_PORT", "8723"))
    bind = os.environ.get("RUN_LEDGER_BIND", "0.0.0.0")
    os.makedirs(_vault_dir(), exist_ok=True)
    httpd = HTTPServer((bind, port), Handler)  # single-threaded: serialised writes
    sys.stderr.write(
        f"[run-ledger] serving vault {_vault_dir()} on {bind}:{port} "
        f"(auth {'on' if TOKEN else 'OFF'})\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
