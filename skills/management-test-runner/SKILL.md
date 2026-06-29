---
name: management-test-runner
description: Stand up an emulated-SA management env from a chosen image source and run management tests in a kept-alive ipython session inside tmux, then triage source edits (yang / cli / routing_manager / quagga) into the minimal rebuild+redeploy. Use when the user says "run management tests", "management test runner", "start a management env and run X", or wants an interactive management test loop with same-image redeploys.
---

# management-test-runner

Drive the management test loop entirely through `scripts/`. Detail lives in `scripts/` and `references/`; this file is the directive checklist.

## Inputs
- `images` (required): `cached` | `latest` | a Jenkins build URL | `pr` (resolve current branch PR build with artifacts).
- `tests` (optional): `suite.Class.method` / `suite.method` targets to run in ipython. If omitted, just bring the env up and open ipython.

## Hard rules
- Run the bring-up in a SUBAGENT. The cleanup + apply + env-watch phase is extremely context-heavy (full builds, image pulls, env logs). Dispatch it to a Task subagent (`subagent_type: shell` or `generalPurpose`) that performs Stage 0 + Stage 2 in the tmux pane and returns ONLY a concise status: env sentinel rc, `emu_sa_*` up count, and (on failure) the failing process + the single most relevant log line/path. The orchestrator must NOT stream raw build/env output into its own context. Run ipython + the test (Stage 3) from the orchestrator after the subagent reports the env is up.
- Overlay needs a CONSISTENT host tree. When compiling (local changes), the env overlays ALL host `lib/*.so`, not just what you rebuilt. Any host lib older than the branch's source (a half-built tree) breaks the device even after CLI/ORM/py_urpc are fixed (e.g. stale `libdn_aaa.so` -> login `BadCredentials`; stale `py_urpc.so` -> boot crash). So an overlay bring-up MUST first bring every overlaid host lib up to date with branch HEAD (rebuild the stale ones), not whack-a-mole one failure at a time.
- NEVER run env/ipython on the internal terminal. Always `tmux send-keys` into a pane.
- ALWAYS resolve the pane with `scripts/mtr_tmux.sh` and use the target it prints. NEVER pick/create a session by hand.
- NEVER create a new tmux session when one already exists. `mtr_tmux.sh` reuses an existing session (new window only if every pane is busy) and creates a session only when `tmux ls` is empty. Never hijack a busy pane.
- Triage the DELTA since the last apply only (`mtr_triage.sh <marker>`), never the cumulative `git diff`. `mtr_apply.sh` touches the marker at its start.
- `REDEPLOY` token requires: exit ipython, apply, relaunch ipython (cli connections break on redeploy). Other tokens apply in place.
- Keep the session and env alive at the end. Never tear down.
- Run each long step via `scripts/watcher.sh` + `AwaitShell` on its sentinel, not fixed sleeps.
- Before a fresh bring-up, run `scripts/mtr_cleanup.sh` to clear stale prior-run state (leftover processes/builder containers AND stale gitignored generated YANG artifacts); it never touches `emu_sa_*`.
- Prefer IMAGE binaries; start a clean env. `mtr_env.sh` brings the env up with `--binaries-volume-type never`, so the device runs the image's own python + native `.so`. Do NOT regenerate CLI/ORM or overlay host source when the working tree is clean: the default `auto` overlay puts branch host-source on top of the image and skews against the image's compiled libs (e.g. `py_urpc.so` missing a renamed symbol, `libevents` bitset size), crashing the device on boot (transaction_agent ImportError, node_manager exit 127). Compile ONLY when local changes are detected (triage) or the test infra forces it.
- When you DO compile, never regenerate CLI without ORM: a `cli_commands` set ahead of the compiled yang/uRPC schema makes `cli_server` crash on an unknown RPC, so the CLI never comes up and every login times out. Pair `GENERATE_CLI` with `BUILD_ORM`.

## Workflow

### Stage 0: Cleanup
- `bash <skill>/scripts/mtr_cleanup.sh` — kills leftovers from prior skill runs (hung `mtr_apply`/`mtr_env`/`watcher`, a stuck `start_emu_sa_env`, and orphaned dbuild `generate-dn-cli-api`/`yang_orm` builder containers that would otherwise hold the pane), and removes stale gitignored generated YANG artifacts (`prod/dnos_monolith/yangs/yang-library-data-gen.json`, `cg/`, `cg_yangs_gen.lock`). It NEVER touches `emu_sa_*` env containers. Watch the `__MTR_CLEANUP_DONE__` sentinel.
- Why the YANG cleanup: `generate-dn-cli-api` consumes `yang-library-data-gen.json` but never regenerates it. A copy left from a previous branch can reference a module the current branch no longer ships (e.g. `dn-srv-port-mirroring@2025-06-23`), and CLI generation then dies with `yangson ... ModuleNotFound`. Deleting the gen set forces `dm.py` to regenerate it from the current branch's tracked yang sources. Do NOT use `dbuild make clean` for this: its `clean::` rules rebuild the whole C/C++/Rust world and don't even touch the gen file.
- Generated-artifact consistency (only when you compile): if local changes force a CLI/ORM rebuild, regenerate CLI and ORM TOGETHER (`GENERATE_CLI BUILD_ORM` alongside `REDEPLOY`). A stale skew (CLI regenerated, ORM not) is the classic cause of `cli_server` crashing on an unknown RPC and the env never reaching a usable CLI. With image binaries (the default), you skip this entirely.

**Gate:** cleanup sentinel rc=0; no stale `mtr_apply`/`mtr_env`/`watcher`/dbuild-make processes and no orphaned builder containers remain; stale generated YANG artifacts removed (or correctly skipped if tracked); `emu_sa_*` (if any) untouched.

### Stage 1: Init
- Resolve repo root (`git rev-parse --show-toplevel`). Create `~/.mtr-runs/<run-id>/` and an empty `last_apply_marker`.
- `PANE=$(bash <skill>/scripts/mtr_tmux.sh)` and use `$PANE` for every send-keys. Validate `images`.

**Gate:** run-dir + marker exist; `$PANE` resolved via `mtr_tmux.sh` (no hand-created session); `images` is valid.

### Stage 2: Bring up env (run inside the bring-up subagent)
- Arg order is `mtr_apply.sh <marker> <images> <tokens...>`.
- Default (clean working tree): image binaries, NO local compile — `bash <skill>/scripts/mtr_apply.sh <marker> <images> REDEPLOY`; `mtr_env.sh` passes `--binaries-volume-type never`. Watch the sentinel.
- Local changes (triage non-empty) => OVERLAY bring-up. Before redeploying, make the overlaid host `lib/` consistent with branch HEAD, otherwise stale `lib/*.so` from a half-built tree crash the device even after CLI/ORM are fresh (stale `py_urpc.so` -> boot crash / node_manager exit 127; stale `libdn_aaa.so` -> login `BadCredentials`). Steps:
  1. Find stale host libs: `ls -la --time-style=+%Y-%m-%d /home/dn/cheetah/lib/*.so` and take those older than the freshly-built ones (i.e. predating your current build session).
  2. Rebuild them in ONE incremental ninja pass (ninja skips any whose source is unchanged since base, so this stays minimal). Map each `lib<X>.so` -> cmake target `<X>` (note multi-target libs: `libsyncd`->`syncd pysyncd`, `libtpm_key_api`->`tpm_key_api tpm_key_init`):
     `dbuild make build_dynamic_target_list DYNAMIC_CMAKE_CONFIG=Release DYNAMIC_TARGET_LIST="dn_aaa em_common_connection idim jag_msgs_utils jag_protobuf_c jag_protobuf probe syncd pysyncd tpm_key_api tpm_key_init"`
     (adjust the list to the stale set you found; a lib still dated old after this is benign if its source is unchanged since the merge-base).
  3. Redeploy with the overlay forced and let the dtest build-deps regenerate CLI/ORM/corm: `MTR_BINARIES_VOLUME_TYPE=always bash <skill>/scripts/mtr_apply.sh <marker> <images> REDEPLOY`. (`mtr_apply` already auto-selects `always` when a compile token is present, but pass it explicitly when you only use REDEPLOY.)
- Verify 14 `emu_sa_*` containers are up AND `start_emu_sa_env::test_emulated_sa` PASSED (default dnroot/dnroot login works) — a healthy boot count alone is not enough; auth must work.

**Gate:** env sentinel rc=0, 14 `emu_sa_*` containers running, and `start_emu_sa_env` passed (no `BadCredentials`).

### Stage 3: Open ipython + run tests
- `make -C src/tests/ run_mgmt_ipython_test_container`; wait for `In [1]:` (see `references/ipython-usage.md`).
- Run each `tests` target; report pass/fail. On no targets, leave the prompt ready.

**Gate:** ipython prompt is live and requested targets have a reported result.

### Stage 4: Change loop
- On a user change signal: `tokens=$(bash <skill>/scripts/mtr_triage.sh <marker>)`.
- If empty, rerun the target. Else `bash <skill>/scripts/mtr_apply.sh <marker> $tokens <images>` (exit/relaunch ipython when tokens include `REDEPLOY`), then rerun affected targets.

**Gate:** triage ran on the delta only; the minimal action was applied; affected targets reran.

### Stage 5: Keep alive
- Print state (session, pane, run-dir, marker, last tokens). Leave session + env running.

## Output format
A short status block: tmux session/pane, image source, container count, per-target pass/fail, and the last triage tokens applied. No teardown.

## Quality bar (self-check)
[ ] `mtr_cleanup.sh` ran before the bring-up; no stale prior-run processes/containers remained, stale gitignored generated YANG artifacts were cleared, and `emu_sa_*` was untouched.
[ ] Bring-up used image binaries (`--binaries-volume-type never`) with no local compile on a clean tree; compiled only on detected local changes, and then regenerated CLI and ORM together (never `GENERATE_CLI` without `BUILD_ORM`).
[ ] Nothing ran on the internal terminal; all env/ipython work went through tmux send-keys.
[ ] An existing tmux session was reused when present; no busy pane was hijacked.
[ ] Triage used `mtr_triage.sh <marker>` (delta since last apply), never a cumulative git diff.
[ ] `REDEPLOY` flows exited ipython before redeploy and relaunched it after.
[ ] The session and `emu_sa_*` env were left alive at the end.
