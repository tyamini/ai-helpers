# Driving the management ipython container

The env must already be up (the ipython profile only *attaches*; it never creates an env).

## Launch (in the chosen tmux pane)
```
make -C src/tests/ run_mgmt_ipython_test_container
```
Wait for the `In [1]:` prompt. The `mgmt` profile auto-connects and exposes:
`cli`, `get_cli_config` (call as `get_cli_config(cli)`), `get_vtysh_config`, `dbclient`,
`redis`, `get_orm`, `wb_agent_client`, `gnmi_client`.

## Project name must match the env
The ipython profile defaults to `PROJECT_NAME=emu_sa`, which matches an env started by
`dtest start_emu_sa_env` (containers `emu_sa_*`). If the env was instead started as
`mgmt_*` (e.g. `dtest management -t ...`), set inside ipython before any container-name op:
```
from management_infra import config; config.PROJECT_NAME = 'mgmt'
```
This skill always brings the env up via `mtr_env.sh` (emu_sa), so no override is needed.

## Run a test method
```
from management.tests import test_<suite>
t = test_<suite>.Test<Class>()
t.<method>(cli, get_vtysh_config)   # pass the fixtures the method declares
```

## Iterate after an edit
```
import importlib; importlib.reload(test_<suite>)
t = test_<suite>.Test<Class>()
t.<method>(cli, get_vtysh_config)
```

## Notes
- `--break`/pdb works here because tmux provides a TTY (it would hang on the internal terminal).
- On `REDEPLOY`, exit ipython first (`exit`), apply, then relaunch — the redeploy recreates
  containers and breaks the live `cli`/`dbclient` connections.
