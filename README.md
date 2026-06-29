# ai-helpers

Personal AI agent helpers — skills, profiles, and rules used by Cursor / coding agents.

## Layout

```
.
├── skills/     # Reusable agent skills (each in its own folder with a SKILL.md)
├── profiles/   # Agent profiles
├── rules/      # Agent rules
└── tools/      # Standalone tooling (services, CLIs) used by skills/agents
```

## Skills

| Skill | Description |
| --- | --- |
| `skills/execution-loop/` | Validate and execute one or more plans, one subagent per plan (sequential) via /implementation-loop, driving each plan to its pass criteria. |
| `skills/iked-failure-rca/` | Root-cause analysis for IKEd test failures. |
| `skills/iked-fix-triage/` | Triage and propose fixes for IKEd failures. |
| `skills/iked-test-loop/` | Iterative test-fix-retest loop for IKEd (runs each test inline in Stage 2c via a tmux pane + sentinel watcher; dispatches `iked-failure-rca` and `iked-fix-triage` on failure). |
| `skills/management-test-runner/` | Stand up an emulated-SA management env from an image source (cached/latest/PR/Jenkins URL), run management tests in a kept-alive ipython session inside tmux, and triage source edits (yang/cli/routing_manager/quagga) into the minimal rebuild+redeploy (marker-based delta). |
| `skills/implementor/` | Generic implementation skill. |
| `skills/ipsec-yang-sync/` | Sync the IPsec YANG/CLI branch from a feature branch. |

Each skill is self-contained; see its `SKILL.md` for usage instructions.

## Tools

| Tool | Description |
| --- | --- |
| `tools/run-ledger/` | Deterministic, non-LLM observability for orchestration loops: per-machine event recorder + spool/flush, a central Markdown/Obsidian-vault ingest service on `tyamini-dev`, and a timeline CLI. |

See each tool's `README.md` for setup and usage.
