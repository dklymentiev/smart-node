# Smart Node

[smart-node.app](https://smart-node.app/) · by [Dmytro Klymentiev](https://klymentiev.com) ([GitHub](https://github.com/dklymentiev))

Smart Node installs a self-hosted AI node: one machine you control, running an AI
assistant together with the pieces it needs to be useful. Task and objective
tracking, a document store, semantic memory, a gateway that fronts your model
provider, an operator panel, and live voice in the browser.

One thing to be clear about before you read further: self-hosted here means the
application and your data, not the model. The node runs on your machine and its
documents and memory stay there. Reasoning currently uses a cloud model through
Anthropic, Google, or OpenRouter, and the node needs credentials for the provider
you choose. Running a model on the node itself is not supported today.

It is bash and Docker. This repository carries the source of every component, so
one clone is everything you need. Nothing has to be built first: point a
single command at a machine and wait about half an hour.

## What works today

- A clean install on a Debian or Ubuntu target, either in a local LXC container
  or on an existing machine.
- The operator panel, Studio, planner, documents, memory, browser voice, and the
  model gateway.
- Email, Telegram, Slack, web chat, and browser voice channels. Google Calendar
  and Drive are partial; WhatsApp, SMS, and arbitrary integrations are not
  turnkey features.
- More than 200 install-time validation checks, plus source and component tests
  in this repository.

## What does not work yet

- No one outside the maintainer's team has completed this repository's install
  runbook yet. Treat the current path as testable software, not a polished
  appliance, and please report every undocumented step.
- A clean install creates the configurator agent, but no business-specific
  agents or workflows. The configurator helps create those after first login.
- There is no one-command updater, automatic migration runner, scheduled backup,
  offline install, local-model runtime, or supported degraded/offline mode.
- It is not an autonomous company. Actions still depend on the permissions,
  approvals, integrations, and instructions you configure.

## Quick start

```sh
git clone https://github.com/dklymentiev/smart-node.git
cd smart-node

# Check the controller before changing anything.
tools/preflight.sh --provider lxd-local

# Create a local LXC and install a node into it.
sudo orchestrator/install-on.sh \
  --provider lxd-local \
  --name <name> --ip <ip> --ssh-key <key.pub> \
  --all --setup-via-token
```

The installer asks one question first: how your AI will work, with a Claude
subscription or with an API key (Anthropic, OpenAI, Google, OpenRouter or your
own address). It installs what that needs; `--ai agent-runtimes` or `--ai key`
answers it on the command line.

### Or in a browser

```sh
sudo python3 tools/web-install/server.py -- \
  --provider lxd-local --name <name> --ip <ip> --ssh-key <key.pub> --setup-via-token
```

It prints an address on `127.0.0.1` with a one-time key in it. Open it: the page
asks the same question, lets you choose what to install (Simple, Full or
Custom), shows each step with the installer's own output beside it, can stop the
install, and opens Studio when it is done. It runs the same installer; it has
been walked through on a local install, and the terminal way above is the one
tested on a server of your own.

For a node on your own computer, Windows with WSL included, see
[A node on your own computer](docs/install-from-source.md#a-node-on-your-own-computer).
For a machine you already have, run the matching preflight and pass
`--provider existing --ssh-user <user>` instead. Nothing is created in that
mode; the installer configures the target you name.

`--all` runs every install layer and ends with validation. In an interactive
terminal it pauses after installing the agent runtime and asks you to authorize
your own Claude account. Without that authorization the command waits at the
prompt and does not reach final validation. Complete the prompt, or cancel it
with `Ctrl-C`; cancellation leaves Claude deferred, continues the install, and
you can connect it later in **Studio -> AI Gateway -> Connect**.

The installer ends with Studio's first-run link. Open it: it creates the owner,
shows your password once (save it; you change it later in Settings), connects
your AI and sets up your first assistant. Model and channel credentials can also
be supplied at install time; `orchestrator/install-on.sh --help` lists the
supported flags.

Read [the source-install runbook](docs/install-from-source.md) before using a
machine that matters. It includes prerequisites, verification, and channel
setup rather than hiding those details behind the short example above.

## How it works

`install-on.sh` runs from your machine and drives the target over SSH. It creates
or takes the machine, hardens the host, lays down the core in six stages, brings
up twelve components in dependency order, applies the declared permission list,
and then runs more than 200 checks before it says it is done.

If a check fails, the install fails. Nothing is reported healthy that has not
been verified.

## Documentation

[docs/README.md](docs/README.md) is the way in: what to read first, and what to
do in the first hour after an install finishes, which is where a node goes from
running to actually working.

[docs/node/](docs/node/) is the set that also ships INTO the node, where its
agents read it: what the machine is made of, the exact call to change each part,
and what each moving part is called. It is written for an agent, which makes it
the densest description of the system for a human too. Each file carries the core
version it was checked against.

[docs/install-from-source.md](docs/install-from-source.md) is the full runbook:
prerequisites, building each image, what to verify, what not to improvise.

[docs/public-domain.md](docs/public-domain.md) covers putting a node on a domain
the outside world can reach. That part is still manual, and doing it from memory
has broken nodes quietly enough that it is worth reading first.

Three runbooks cover the day after installation:
[backup](docs/runbook-backup.md), [restore](docs/runbook-restore.md) and
[update](docs/runbook-update.md). Backup and restore have been run end to end on
a throwaway node and corrected from what that found; update has not. Each says
which at the top. Do the drill yourself before you rely on any of them on a
machine that matters.

[SECURITY.md](SECURITY.md) explains what an agent can and cannot do to the node
it runs on, and says plainly which limits are enforced by the operating system
and which are not.

[NOTICE.md](NOTICE.md) lists every component vendored here with its origin and
licence. Read it before editing a component: the copy in this tree is a pinned
snapshot, not that component's home.

[CONTRIBUTING.md](CONTRIBUTING.md) covers builds, conventions and how to propose
a change.

On the node itself, `sn-version` reports what is actually running rather than
what was meant to be, hashes it into a fingerprint, and pins it. `sn-version
check` tells you when a node has drifted from that pin.

## Providers

`--provider lxd-local` creates a local LXC container and installs into it. Good
for a self-contained node on a single Ubuntu host with LXD.

`--provider existing` takes a machine you already have, whether bare metal, a
VPS, or a container you made yourself. Nothing is created; the installer
configures what you point it at.

## Known limits

There is no one-command updater or automatic migration runner.
[docs/runbook-update.md](docs/runbook-update.md) describes the current operator
procedure. It is a procedure, not a product feature.

Putting a node on a public domain is a manual procedure. It is documented, but
not automated.

The install is not offline. It fetches operating-system and build dependencies,
so the controller and target need internet during setup. A model credential is
required before an agent can answer usefully.

## Licence

Copyright 2026 Dmytro Klymentiev. Licensed under AGPL-3.0-or-later; see
[LICENSE](LICENSE).

In practice this means: run it, modify it, use it inside your company, for free
and without telling anyone. If you offer a modified version to other people over
a network as a service, you owe those people your changes. Nothing is owed to me
either way.

This tree also carries pinned snapshots of the components a node is built from.
Five of them were released separately under MIT and keep that licence on their
own; the rest are covered by the root licence. [NOTICE.md](NOTICE.md) lists each
one, and says what this repository does not license: dependencies fetched at
build time, the external mail image, and your model provider's terms.
