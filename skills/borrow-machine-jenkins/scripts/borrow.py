#!/usr/bin/env python3
"""Borrow / return / extend a CI machine via the Jenkins BorrowMachine job.

Drives the Jenkins REST API headlessly so you don't have to click through the
web form: trigger the build -> poll the queue -> poll the build -> parse the
console for the allocated machine and SSH details.

Authentication
--------------
The borrow is tied to whoever triggers the build, so the script must
authenticate as you. Provide a Jenkins API token (create one once at
https://jenkins.dev.drivenets.net/me/configure):

    export JENKINS_USER="<your-jenkins-username>"
    export JENKINS_API_TOKEN="<token>"

Alternatively put the same KEY=VALUE lines in
~/.config/borrow-machine-jenkins.env (chmod 600).

Examples
--------
    # Borrow a random medium machine for 4 hours and wait for it
    borrow.py borrow --type medium --lease 4

    # Borrow a specific j2 machine, start the environment, don't block
    borrow.py borrow --type j2 --machine WDY1A77B0004E --start-env --no-wait

    # Return / extend
    borrow.py return --machine WDY1A77B0004E
    borrow.py extend --machine WDY1A77B0004E --lease 8
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://jenkins.dev.drivenets.net"
DEFAULT_JOB = "BorrowMachine"
CREDS_FILE = os.path.expanduser("~/.config/borrow-machine-jenkins.env")

MACHINE_TYPES = [
    "tiny", "small", "medium", "large", "orm_builder",
    "j2", "j2_beta", "j2_beta_spirent", "j2_ncp3", "j2_ncpl",
    "j3ai", "q3d", "emux", "emux_s", "cluster", "cluster_beta",
    "baseos_tester", "ai3_tester", "ai_cluster",
]


# ── output helpers ────────────────────────────────────────────────

def info(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "None":
    print(f"[FAIL] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ── credentials ───────────────────────────────────────────────────

def load_creds() -> tuple[str, str]:
    user = os.environ.get("JENKINS_USER")
    token = os.environ.get("JENKINS_API_TOKEN")
    if (not user or not token) and os.path.isfile(CREDS_FILE):
        with open(CREDS_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if key.strip() == "JENKINS_USER" and not user:
                    user = val
                elif key.strip() == "JENKINS_API_TOKEN" and not token:
                    token = val
    if not user or not token:
        die(
            "missing Jenkins credentials. Set JENKINS_USER and JENKINS_API_TOKEN "
            f"in the environment or in {CREDS_FILE}. Create a token at "
            f"{DEFAULT_BASE_URL}/me/configure"
        )
    return user, token


# ── HTTP ──────────────────────────────────────────────────────────

class Jenkins:
    def __init__(self, base_url: str, user: str, token: str):
        self.base = base_url.rstrip("/")
        auth = base64.b64encode(f"{user}:{token}".encode()).decode()
        self.auth_header = f"Basic {auth}"
        self._crumb: tuple[str, str] | None = None

    def _request(self, url: str, *, data: bytes | None = None,
                 headers: dict | None = None, timeout: int = 30):
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("Authorization", self.auth_header)
        for key, val in (headers or {}).items():
            req.add_header(key, val)
        return urllib.request.urlopen(req, timeout=timeout)

    def crumb(self) -> dict:
        if self._crumb is None:
            try:
                resp = self._request(f"{self.base}/crumbIssuer/api/json")
                body = json.load(resp)
                self._crumb = (body["crumbRequestField"], body["crumb"])
            except (urllib.error.HTTPError, KeyError):
                self._crumb = ("", "")  # CSRF disabled
        field, value = self._crumb
        return {field: value} if field else {}

    def get_json(self, path_or_url: str, timeout: int = 30) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base}{path_or_url}"
        resp = self._request(url, timeout=timeout)
        return json.load(resp)

    def get_text(self, path_or_url: str, timeout: int = 60) -> str:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base}{path_or_url}"
        resp = self._request(url, timeout=timeout)
        return resp.read().decode("utf-8", errors="replace")

    def trigger(self, job: str, params: dict[str, str]) -> str:
        """POST buildWithParameters; return the queue-item URL."""
        url = f"{self.base}/job/{urllib.parse.quote(job)}/buildWithParameters"
        data = urllib.parse.urlencode(params).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        headers.update(self.crumb())
        try:
            resp = self._request(url, data=data, headers=headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in (401, 403):
                die(f"auth/permission error ({exc.code}) triggering build. "
                    f"Check JENKINS_USER/JENKINS_API_TOKEN. {detail}")
            die(f"failed to trigger build (HTTP {exc.code}): {detail}")
        location = resp.headers.get("Location")
        if not location:
            die("build accepted but Jenkins returned no queue Location header")
        return location.rstrip("/")


# ── polling ───────────────────────────────────────────────────────

def wait_for_build(jk: Jenkins, queue_url: str, poll: int, timeout: int) -> str:
    """Wait for the queue item to start; return the build URL."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = jk.get_json(f"{queue_url}/api/json")
        if item.get("cancelled"):
            die("build request was cancelled in the Jenkins queue")
        executable = item.get("executable")
        if executable and executable.get("url"):
            return executable["url"].rstrip("/")
        why = item.get("why") or "queued"
        info(f"  ...waiting in queue: {why}")
        time.sleep(poll)
    die(f"timed out after {timeout}s waiting for the build to leave the queue")


def wait_for_result(jk: Jenkins, build_url: str, poll: int, timeout: int) -> dict:
    """Wait until the build finishes; return its info dict."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        binfo = jk.get_json(f"{build_url}/api/json?tree=building,result,number")
        if not binfo.get("building") and binfo.get("result") is not None:
            return binfo
        info("  ...build running")
        time.sleep(poll)
    die(f"timed out after {timeout}s waiting for the build to finish. "
        f"It may still be running: {build_url}")


# ── console parsing ───────────────────────────────────────────────

def parse_console(text: str) -> dict:
    """Extract the allocated machine, borrower, lease end and SSH hint."""
    result: dict = {}

    machine = None
    m = re.search(r"dn@([A-Za-z0-9][A-Za-z0-9._-]+)'", text)
    if m:
        machine = m.group(1)
    if not machine:
        m = re.search(r"\b([A-Za-z0-9][A-Za-z0-9._-]{3,}) is reserved, proceeding", text)
        if m:
            machine = m.group(1)
    if not machine:
        m = re.search(r"Node ([A-Za-z0-9][A-Za-z0-9._-]{3,}) does not have 'borrow' labels", text)
        if m:
            machine = m.group(1)
    result["machine"] = machine

    borrowers = [b for b in re.findall(r"taken_by_([A-Za-z0-9._-]+)", text) if b != "drivenets"]
    result["borrower"] = borrowers[-1] if borrowers else None

    lease_ends = re.findall(r"lease_end_(\d+)", text)
    if lease_ends:
        ts = int(lease_ends[-1])
        result["lease_end_epoch"] = ts
        result["lease_end"] = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

    m = re.search(r'Now try logging into the machine, with:\s*"(.+?)"', text)
    if m:
        result["ssh_hint"] = m.group(1)
    return result


def print_borrow_result(build_url: str, parsed: dict) -> None:
    print("")
    print("=" * 60)
    print("  MACHINE BORROWED")
    print("=" * 60)
    machine = parsed.get("machine")
    if machine:
        print(f"  Machine : {machine}")
    if parsed.get("borrower"):
        print(f"  Borrower: {parsed['borrower']}")
    if parsed.get("lease_end"):
        print(f"  Lease until: {parsed['lease_end']}")
    print("")
    if machine:
        print("  SSH (port 2222, user dn / pass drivenets, or dnroot / dnroot):")
        print(f"    ssh -p 2222 dn@{machine}")
    elif parsed.get("ssh_hint"):
        print(f"  SSH: {parsed['ssh_hint']}")
    print(f"  Build: {build_url}")
    print("=" * 60)


# ── status ────────────────────────────────────────────────────────

def list_borrowed(jk: Jenkins) -> list[tuple[str, str | None, int | None]]:
    """Return [(machine, borrower, lease_end_epoch)] from the node labels."""
    data = jk.get_json(
        "/computer/api/json?tree=computer[displayName,assignedLabels[name]]"
    )
    rows: list[tuple[str, str | None, int | None]] = []
    for comp in data.get("computer", []):
        name = comp.get("displayName")
        borrower: str | None = None
        lease: int | None = None
        for lbl in comp.get("assignedLabels") or []:
            label = lbl.get("name", "")
            if label.startswith("taken_by_") and label != "taken_by_drivenets":
                borrower = label[len("taken_by_"):]
            elif label.startswith("lease_end_"):
                try:
                    lease = int(label[len("lease_end_"):])
                except ValueError:
                    pass
        if borrower or lease:
            rows.append((name, borrower, lease))
    return rows


def print_borrowed(rows: list[tuple[str, str | None, int | None]]) -> None:
    if not rows:
        print("No borrowed machines found.")
        return
    now = time.time()
    print(f"{'MACHINE':32} {'BORROWER':16} {'LEASE END':20} REMAINING")
    for name, borrower, lease in sorted(rows, key=lambda r: (r[1] or '', r[0])):
        if lease:
            end = _dt.datetime.fromtimestamp(lease).strftime("%Y-%m-%d %H:%M:%S")
            rem = lease - now
            remaining = (
                f"{int(rem // 3600)}h{int((rem % 3600) // 60)}m"
                if rem > 0 else "EXPIRED"
            )
        else:
            end = "?"
            remaining = "?"
        print(f"{name:32} {str(borrower):16} {end:20} {remaining}")


# ── actions ───────────────────────────────────────────────────────

def build_params(args: argparse.Namespace) -> dict[str, str]:
    if args.action == "borrow":
        params = {
            "Action": "Borrow",
            "MACHINE_TYPE": args.type,
            "SPECIFIC_MACHINE": args.machine or "",
            "LEASE_TIME": str(args.lease),
            "REPOSITORY": args.repository,
            "join_the_queue": "true" if args.queue else "false",
            "SKIP_BASEOS_REPLACEMENT": "true" if args.skip_baseos else "false",
            "start_env": "true" if args.start_env else "false",
            "allow_multiple_borrow": "true" if args.allow_multiple else "false",
            "GIT_BRANCH": args.branch or "",
            "JENKINS_JOB": args.jenkins_job or "",
        }
    elif args.action == "return":
        params = {"Action": "Return", "RETURN_SLAVE": args.machine}
    elif args.action == "extend":
        params = {
            "Action": "Extend",
            "EXTEND_SLAVE": args.machine,
            "NEW_LEASE_TIME": str(args.lease),
        }
    else:  # pragma: no cover
        die(f"unknown action {args.action}")
    return params


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Borrow/return/extend a CI machine via the Jenkins BorrowMachine job.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--job", default=DEFAULT_JOB)
    parser.add_argument("--no-wait", action="store_true",
                        help="Trigger the build and print the URL without waiting.")
    parser.add_argument("--timeout", type=int, default=2700,
                        help="Max seconds to wait for the build to finish (default 2700).")
    parser.add_argument("--poll-interval", type=int, default=15)
    parser.add_argument("--print-build-url-file", default=None,
                        help="If set, write the build URL to this file once known.")
    parser.add_argument("--print-machine-file", default=None,
                        help="If set, write the allocated machine name to this file on a "
                             "successful borrow (for scripting).")

    sub = parser.add_subparsers(dest="action", required=True)

    p_borrow = sub.add_parser("borrow", help="Borrow a machine from a pool by type.")
    p_borrow.add_argument("--type", required=True, choices=MACHINE_TYPES,
                          help="Machine type / pool.")
    p_borrow.add_argument("--machine", default=None,
                          help="Specific machine name (default: random from the pool).")
    p_borrow.add_argument("--lease", default="2", help="Lease time in hours (default 2).")
    p_borrow.add_argument("--repository", default="cheetah")
    p_borrow.add_argument("--branch", default=None, help="GIT_BRANCH to checkout.")
    p_borrow.add_argument("--jenkins-job", default=None,
                          help="Jenkins build URL to configure the env from (JENKINS_JOB).")
    p_borrow.add_argument("--start-env", action="store_true")
    p_borrow.add_argument("--queue", action="store_true",
                          help="Join the queue and wait for a machine to free up.")
    # BaseOS replacement is the slow, timeout-prone stage of the borrow
    # pipeline, so skip it by default. Pass --no-skip-baseos to force a clean
    # OS reinstall on the borrowed node.
    p_borrow.add_argument("--skip-baseos", dest="skip_baseos", action="store_true",
                          default=True,
                          help="Skip the BaseOS replacement stage (default: enabled).")
    p_borrow.add_argument("--no-skip-baseos", dest="skip_baseos", action="store_false",
                          help="Run the BaseOS replacement stage (slower; forces a clean OS).")
    # Allow the same user to hold multiple setups at once. Enabled by default;
    # pass --no-allow-multiple to restore Jenkins' stricter single-setup rule.
    p_borrow.add_argument("--allow-multiple", dest="allow_multiple",
                          action="store_true", default=True,
                          help="Allow the same user to borrow multiple setups (default: enabled).")
    p_borrow.add_argument("--no-allow-multiple", dest="allow_multiple",
                          action="store_false",
                          help="Disallow borrowing multiple setups at once.")

    p_return = sub.add_parser("return", help="Return a borrowed machine.")
    p_return.add_argument("--machine", required=True, help="Machine name to return.")

    p_extend = sub.add_parser("extend", help="Extend the lease on a borrowed machine.")
    p_extend.add_argument("--machine", required=True, help="Machine name to extend.")
    p_extend.add_argument("--lease", default="1", help="Additional/new lease hours (default 1).")

    p_mine = sub.add_parser("mine", help="List machines currently borrowed by you.")
    p_mine.add_argument("--all", action="store_true",
                        help="Show all borrowed machines, not just yours.")

    args = parser.parse_args()

    user, token = load_creds()
    jk = Jenkins(args.base_url, user, token)

    if args.action == "mine":
        rows = list_borrowed(jk)
        if not args.all:
            rows = [r for r in rows if r[1] == user]
            if not rows:
                print(f"No machines currently borrowed by '{user}'.")
                return 0
        print_borrowed(rows)
        return 0

    params = build_params(args)

    info(f"Triggering {args.job}: {params}")
    queue_url = jk.trigger(args.job, params)
    info(f"Queued: {queue_url}")

    build_url = wait_for_build(jk, queue_url, args.poll_interval, timeout=600)
    info(f"Build started: {build_url}")
    if args.print_build_url_file:
        try:
            with open(args.print_build_url_file, "w", encoding="utf-8") as fh:
                fh.write(build_url + "\n")
        except OSError:
            pass

    if args.no_wait:
        print(build_url)
        return 0

    binfo = wait_for_result(jk, build_url, args.poll_interval, timeout=args.timeout)
    result = binfo.get("result")
    console = jk.get_text(f"{build_url}/consoleText")

    if result != "SUCCESS":
        tail = "\n".join(console.splitlines()[-25:])
        die(f"build finished with result={result}. Last lines:\n{tail}\n{build_url}",
            code=2)

    if args.action == "borrow":
        parsed = parse_console(console)
        print_borrow_result(build_url, parsed)
        if parsed.get("machine") and args.print_machine_file:
            try:
                with open(args.print_machine_file, "w", encoding="utf-8") as fh:
                    fh.write(parsed["machine"] + "\n")
            except OSError:
                pass
        if not parsed.get("machine"):
            info("WARNING: could not parse the machine name from the console; "
                 f"check the build manually: {build_url}")
    else:
        print(f"[OK] {args.action} succeeded: {build_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
