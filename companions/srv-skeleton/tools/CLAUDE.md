# /srv/tools — tool convention

A **tool** is a reusable capability callable by agents and
from Claude Code. In Smart Node, tools are registered in the database
(`sn_config.tools` + `sn_config.integrations`); the filesystem layout here is
the projection used at runtime.

## Structure

Each tool = one folder with `*.sh` wrappers and its own `CLAUDE.md` holding
invocation examples:

```
<tool-name>/
├── CLAUDE.md     # what it does + example calls
└── *.sh          # thin wrappers (no business logic, no hardcoded secrets)
```

No concrete tools ship in the base — only this convention. Tools appear as the
registry is populated through the panel.
