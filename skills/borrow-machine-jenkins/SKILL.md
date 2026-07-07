---
name: borrow-machine-jenkins
description: Borrow, return, or extend a CI machine from the Jenkins BorrowMachine pool by type (tiny/small/medium/large/j2/cluster/...), instead of clicking through the web form. Triggers the Jenkins job headlessly via the REST API, waits for it, and reports the allocated machine name plus SSH details. Use when the user says "borrow a machine from Jenkins", "borrow a CI machine", "borrow a medium/j2/cluster machine", "return my borrowed machine", or "extend my machine lease".
user_invocable: true
---

# Borrow Machine (Jenkins)

## Goal
Borrow (or return/extend) a CI machine from the Jenkins `BorrowMachine` pool
by type, headlessly, and hand the user the allocated machine name and SSH
command. This is distinct from the `borrow-machine` skill, which borrows a
specific named machine via the local `pyborrow.py` (dp-tools). This is a
private (local-only) skill under `AI/private/`.

## Prerequisites
- The borrow is tied to **who triggers the Jenkins build**, so a personal
  Jenkins API token is required (anonymous access cannot borrow as you).
- Token setup (one time): open `https://jenkins.dev.drivenets.net/me/configure`,
  add an API token, then export it:
  ```bash
  export JENKINS_USER="<your-jenkins-username>"
  export JENKINS_API_TOKEN="<token>"
  ```
  or put the same `KEY=VALUE` lines in `~/.config/borrow-machine-jenkins.env`
  (chmod 600). The script reads the environment first, then that file.

## Workflow

### Stage 1: Resolve the request
- Determine the action: **borrow** (default), **return**, or **extend**.
- For **borrow**, resolve the machine type. Valid types: `tiny`, `small`,
  `medium`, `large`, `orm_builder`, `j2`, `j2_beta`, `j2_beta_spirent`,
  `j2_ncp3`, `j2_ncpl`, `j3ai`, `q3d`, `emux`, `emux_s`, `cluster`,
  `cluster_beta`, `baseos_tester`, `ai3_tester`, `ai_cluster`.
  Sizes: tiny=8GB/2c, small=12GB/4c, medium=30GB/6c.
- If the user did not give a type, ask which type (and lease hours, default 2).
- For **return**/**extend**, get the machine name from the user.

**Gate:** Action is known; for borrow the machine type is a valid choice; for return/extend a machine name is provided.

### Stage 2: Verify credentials
- Confirm `JENKINS_USER` and `JENKINS_API_TOKEN` are available (env or
  `~/.config/borrow-machine-jenkins.env`).
- If missing, stop and give the user the token-setup steps from Prerequisites.
  Do NOT print or store the token value.

**Gate:** Credentials are resolvable, or the user has been told exactly how to set them.

### Stage 3: Trigger and wait
- Run the driver script with the resolved arguments:
  ```bash
  python3 AI/private/borrow-machine-jenkins/scripts/borrow.py \
      borrow --type <TYPE> [--lease <HOURS>] [--machine <NAME>] [--start-env]
  ```
- **BaseOS replacement is skipped by default.** It is the slowest,
  most timeout-prone stage of the borrow pipeline (it frequently aborts the
  build on slow nodes, leaving a half-configured machine reserved). Skipping
  it makes borrows faster and far more reliable. Only pass `--no-skip-baseos`
  when you specifically need a clean OS reinstall on the node.
  Return / extend:
  ```bash
  python3 AI/private/borrow-machine-jenkins/scripts/borrow.py return --machine <NAME>
  python3 AI/private/borrow-machine-jenkins/scripts/borrow.py extend --machine <NAME> --lease <HOURS>
  ```
- The script triggers the build, polls the Jenkins queue, then polls the build
  until it finishes. A full borrow can take 10-30 min (BaseOS replacement),
  so the default wait timeout is 45 min. Add `--no-wait` to only trigger and
  print the build URL.
- Do not re-trigger on slowness; the build is already queued. Only retry on an
  explicit failure.

**Gate:** The script reported the build finished, or printed a build URL (with `--no-wait`), or exited with a clear failure the user must act on.

### Stage 4: Report
- On success (borrow): report the allocated machine name, the SSH command
  (`ssh -p 2222 dn@<machine>`, user `dn`/`drivenets` or `dnroot`/`dnroot`),
  the lease expiry, and the build URL. Remind the user NOT to reboot the
  machine (state is lost) and that a Slack message also confirms readiness.
- On success (return/extend): confirm the action and link the build.
- On failure: surface the script's reason and the build URL. Common cases:
  already borrowed by someone else, no free machine in the pool (suggest
  `--queue` to wait), or an auth error (token).

## Output format
- Primary: the script prints a result block to stdout. For a borrow:
  ```
  ============================================================
    MACHINE BORROWED
  ============================================================
    Machine : <NODE_NAME>
    Borrower: <user>
    Lease until: <YYYY-MM-DD HH:MM:SS>

    SSH (port 2222, user dn / pass drivenets, or dnroot / dnroot):
      ssh -p 2222 dn@<NODE_NAME>
    Build: https://jenkins.dev.drivenets.net/job/BorrowMachine/<N>/
  ============================================================
  ```
- Progress and credential/queue messages go to stderr.
- Exit codes: `0` success, `1` usage/auth/setup error, `2` the Jenkins build
  itself failed (machine not borrowed).

## Quality bar (self-check)
[ ] Action resolved and, for borrow, the machine type validated against the allowed list.
[ ] Credentials checked before triggering; token value never printed or committed.
[ ] Exactly one build triggered per request (no re-trigger on slowness).
[ ] On a successful borrow, the allocated machine name and `ssh -p 2222` command were reported.
[ ] The build URL is included in every outcome (success or failure).
[ ] Failures (already borrowed / no free machine / auth) are surfaced with the build URL, not retried blindly.
