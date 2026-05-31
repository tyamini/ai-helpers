# ai-helpers

Personal AI agent helpers — skills, profiles, and rules used by Cursor / coding agents.

## Layout

```
.
├── skills/     # Reusable agent skills (each in its own folder with a SKILL.md)
├── profiles/   # Agent profiles
└── rules/      # Agent rules
```

## Skills

| Skill | Description |
| --- | --- |
| `skills/iked-failure-rca/` | Root-cause analysis for IKEd test failures. |
| `skills/iked-fix-triage/` | Triage and propose fixes for IKEd failures. |
| `skills/iked-test-loop/` | Iterative test-fix-retest loop for IKEd (runs each test inline in Stage 2c via a tmux pane + sentinel watcher; dispatches `iked-failure-rca` and `iked-fix-triage` on failure). |
| `skills/implementor/` | Generic implementation skill. |
| `skills/ipsec-yang-sync/` | Sync the IPsec YANG/CLI branch from a feature branch. |

Each skill is self-contained; see its `SKILL.md` for usage instructions.
