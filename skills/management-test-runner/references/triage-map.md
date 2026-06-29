# Triage map: changed path -> action tokens

`mtr_triage.sh` lists files newer than the apply marker (`find <root> -newer <marker>`)
and emits tokens. This is the DELTA since the last apply, never the cumulative git diff.

| Changed root | Tokens | Why |
| --- | --- | --- |
| `prod/dnos_monolith/yangs/**` | `GENERATE_CLI` `BUILD_ORM` `REDEPLOY` | Device reads the host `/yangs` mount; redeploy reloads the schema-loading processes; CLI/ORM must be regenerated to match. |
| `prod/dnos_monolith/autogen_cli/**` | `GENERATE_CLI` `RELOAD_IPYTHON` | CLI command set is regenerated host-side and volume-mounted; only the ipython client needs to reload. No device redeploy. |
| `src/py_packages/routing_manager/**` | `RESTART_ROUTING_MANAGER` | Pure Python, volume-mounted into RE; restart the process in place. No redeploy. |
| `services/control/**` (quagga) | `BUILD_QUAGGA` `REDEPLOY` | Rebuild the quagga debug deb; RE/NM pick it up via `DN_QUAGGA_DEBUG_PATH` only on redeploy. |

Rules:
- Mixed edits -> union of tokens.
- `REDEPLOY` supersedes `RELOAD_IPYTHON` and `RESTART_ROUTING_MANAGER` (a full redeploy already covers them).
- Apply order (in `mtr_apply.sh`): `BUILD_QUAGGA` -> `GENERATE_CLI` -> `BUILD_ORM` -> `RESTART_ROUTING_MANAGER` -> `RELOAD_IPYTHON` -> `REDEPLOY`.

## Build-step stall guard
`BUILD_QUAGGA` / `GENERATE_CLI` / `BUILD_ORM` run under `run_guarded` in `mtr_apply.sh`:
the step is launched in the background, its output is live-streamed to the pane, and a
watchdog watches for OUTPUT INACTIVITY. If there is no new output for `MTR_STALL_SECS`
(default 600s) the builder is considered wedged (rust step waiting on sccache / the remote
build-cache). The guard then `docker stop`s the builder container(s) that step spawned
(never the `emu_sa_*`/`mgmt_*` env), runs `sccache --stop-server`, and retries the command
once. A plain `timeout` is intentionally NOT used: it cannot kill the underlying `docker run`.

## Token -> command
- `BUILD_QUAGGA` -> `dbuild make quagga`
- `GENERATE_CLI` -> `dbuild make generate-dn-cli-api`
- `BUILD_ORM` -> `dbuild make orm` (NOT `yang_orm`; there is no `make yang_orm` target. `orm` = `so_orm-cache || orm-so`. Note `dtest start_emu_sa_env` also builds `yang_orm` as a build-dependency during REDEPLOY, so the device gets a fresh ORM regardless.)
- `RESTART_ROUTING_MANAGER` -> `docker exec <*_routing-engine_1> supervisorctl restart <routing_manager>`
- `RELOAD_IPYTHON` -> caller exits + relaunches the ipython container (`__MTR_RELOAD_IPYTHON__` signal)
- `REDEPLOY` -> `mtr_env.sh <images-spec>`
