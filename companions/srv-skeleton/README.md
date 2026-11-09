# /srv — Smart Node base

This is the agent-system base of a Smart Node deployment. Humans read this
README; agents and Claude Code read the `CLAUDE.md` files.

- **Registry source of truth:** the database (`sn_config`), edited through the
  Smart Node panel. The files here that hold agent data are generated from it.
- **Static skeleton** (installer): `agents/_template/`, `scripts/lib/`, the
  convention `CLAUDE.md` files, this layout.
- **Second layer** (installed on top, not part of this base): planner, mesh,
  rein, gateway, doci, channels, containers, services.

See `CLAUDE.md` for the universal rules and directory layout.
