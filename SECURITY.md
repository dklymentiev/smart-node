# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities privately. Do not open a public issue or
pull request for them.

Use **GitHub private vulnerability reporting**: the repository's *Security* tab
-> *Report a vulnerability*. The report is visible only to the maintainers, you
can follow its status, and a fix ships with an advisory from the same place.

Include as much detail as you can: affected component and version, a
description of the issue, and steps to reproduce or a proof of concept. We will
acknowledge your report and work with you on a fix and a coordinated
disclosure timeline.

> **Maintainer note:** private vulnerability reporting is off by default. Enable
> it in *Settings -> Advanced Security -> Private vulnerability reporting*
> before publishing, or the tab this document points at will not exist and a
> reporter with nowhere to go will open a public issue instead.

## How a node protects itself

A Smart Node runs autonomous agents on a machine that also holds the operator's
credentials. The security model exists for one question: **what can an agent do
to the node it runs on?** The answer has to come from the operating system, not
from a check inside code the agent could reach.

### Two groups, and one rule between them

| Group | Members |
|---|---|
| `smart-node` | service accounts (`smart-node-core-api`, `smart-node-gateway`, ...) |
| `smart-node-agents` | agent accounts (`agent-<slug>`) |

A service account **may** join the agent group where it genuinely shares state
with agents: core-api provisions their model credential, service crons write to
the same log tree. An agent account may **never** join the service group.

That asymmetry is the boundary. Most group-write in the node tree is granted to
`smart-node`, and it is safe only because no agent is in it.

### The permission surface is declared, not decided per component

`bootstrap/permissions.list` is the list itself: path, owner, group, mode and the
reason, in one readable table. It is the only place where "who may write here" is
decided. Companion installers create directories; they do not choose their
permissions.

`bootstrap/permissions.sh` applies it. There are three outcomes per entry. A path
that matches passes silently. A path that is declared but wrong is corrected. A
path that is writable and not declared at all is refused: the stage never guesses
permissions nobody wrote down, so it stops and the declaration has to be added.

It runs as the closing step of every install path, the full installer and a
single-component update alike, so a component re-run cannot reopen a hole on a
node the operator believes is protected. The same check runs daily in report-only
mode, because a routine check may not repair anything and must never be the thing
that takes a node down, and it runs again inside `validate.sh`.

Check any node at any time:

```sh
sudo /usr/local/lib/smart-node/permissions.sh --verify   # reports, changes nothing
sudo /usr/local/lib/smart-node/permissions.sh            # applies the list
```

### What is NOT enforced, so that nobody relies on it

The agent record carries `fs_read`, `fs_write` and `forbidden_fs`. **These are
not enforced by the operating system.** They render into the agent's prompt and
reach it as text: a request, not a boundary. Treat them as documentation.
Anything that must actually be prevented belongs in `permissions.list`, where
the kernel enforces it.

### Recommended operator hardening

1. Register **two** passkeys, at least one portable (a password manager or a
   phone), so losing a device does not lock you out.
2. Then turn password login off by setting `AUTHUM_PASSWORD_LOGIN=off` on the
   Authum container. The endpoint refuses server-side rather than merely hiding
   the form. Recovery afterwards needs shell access to the node: there is no
   email reset and no recovery code, on purpose.

## Supported versions

Smart Node is published from the `main` branch. Security fixes are applied to
the latest released version. Older versions are not maintained, so please update
before reporting an issue.

| Version | Supported |
|---------|-----------|
| latest (`main`) | yes |
| older releases  | no  |
