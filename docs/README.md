# Documentation

Two audiences, kept apart on purpose.

**`docs/`** is for the person who installs and runs a node.
**`docs/node/`** is for the agents living inside one. That same set is loaded into
the node's document system at install, so an agent can read it there; the copy
here is the source, not a duplicate.

## If you have just downloaded this

1. **[../README.md](../README.md)** says what a Smart Node is and what it is not.
2. **[node/index.md](node/index.md)** is the shortest honest description of what
   you are about to install: what runs on the machine, who decides what, where
   the boundaries are. It is written for an agent, which makes it the densest
   thing here for a human too.
3. **[install-from-github.md](install-from-github.md)** installs from a published
   release: one command, no build host. This is the ordinary path.
   [install-from-source.md](install-from-source.md) builds every component
   yourself instead, and is the longer road.
4. **After it says done**, read *First hour* below. An installed node is not yet
   a working one, and the gap between those two is where people give up.

## First hour

Installation ends with a node that runs and does nothing, because nothing has
been asked of it yet. In order:

- **Log in.** The panel is the configurator, the Studio is where work happens.
  Both sit behind the node's own sign-on.
- **Check what you actually got.** `sn-version` on the node reports what is
  running rather than what was intended, and `sn-version check` tells you when it
  has drifted since.
- **Give the agents a brain.** Agents need model credentials, held by the gateway,
  never by the agents themselves. Until then an agent answers with nothing at all,
  which reads like a broken model and is not one.
- **Connect one channel and prove it.** Pick email or chat, bind one address to
  one agent, then send a real message and read the real reply. Do not accept a
  configuration row as evidence. Everything else is easier once one round trip
  works, and most problems show up here.
- **Decide what the node is for.** `templates/company-card.md` in the node's
  documents is the interview: what the business does, who its people are, what
  should happen without being asked.

## Reference

| Document | Answers |
|---|---|
| [node/index.md](node/index.md) | Where am I, who decides, what proves a thing works |
| [node/entities.md](node/entities.md) | What the node is made of, and the exact call to change each part |
| [node/components.md](node/components.md) | What each moving part is, and which name means which thing |
| [install-from-github.md](install-from-github.md) | Install from a published release, one command |
| [install-from-source.md](install-from-source.md) | Build and install |
| [public-domain.md](public-domain.md) | Putting a node on a domain the world can reach |
| [runbook-backup.md](runbook-backup.md) | Backing it up |
| [runbook-restore.md](runbook-restore.md) | Getting it back |
| [runbook-update.md](runbook-update.md) | Updating it |
| [upgrade/](upgrade/) | What each version needs of you, one note per release |
| [../SECURITY.md](../SECURITY.md) | What an agent can and cannot do to its node |
| [../NOTICE.md](../NOTICE.md) | What is vendored here, from where, under what licence |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Builds, conventions, proposing a change |

## Version stamps

The documents under `node/` ship with the release the node runs. If one disagrees
with the running node, believe the node and treat the document as drifted. Documents that cannot go stale loudly go stale
quietly, and a confident wrong sentence costs more than a missing one.
