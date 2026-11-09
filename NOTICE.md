# Notice: what this repository carries, and under what terms

This repository is not only the installer: it carries the source of every
component a node is built from. That makes one clone the whole input for a build,
and it also means the tree contains code that is a program in its own right. This
file says which code, where its source of truth is, and under which licence, so
the root [LICENSE](LICENSE) is not asked to speak for material it does not cover.

The product components listed below are first-party code held by Dmytro
Klymentiev. A small set of browser libraries is redistributed under permissive
third-party licences; [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) identifies
those libraries and the licence copy or retained header for each one.

Two licences are in play, and the table below says which applies to what. The
root LICENSE is AGPL-3.0 and covers the installer and every component that does
not carry its own licence file. Five components were released separately under
MIT before this repository existed and keep that licence where they are taken on
their own; inside this tree they are part of an AGPL-3.0 whole, which permissive
licences allow.

## Where each component's source of truth is

**Most components are edited here.** Their directory in this tree is the source:
you change the file, you commit it to this repository, and that is the whole
process. They once had separate repositories, which were archived on 2026-08-15
because two places for one program is a trap: for a month every change landed
here while the other place stood still and still looked primary. Each such
component carries a `VENDORED` marker naming the archived repository and its last
commit there, so the history remains findable. Archived means read-only and still
clonable, not deleted.

**Four components keep their own repositories** because they are separate
products with a life outside a node: documents (doci), memory (mesh), workflows
(rein) and tracking (planner). For those the copy here is a pinned snapshot, the
`VENDORED` marker records which one, and refreshing it is a real operation with
real rules -- see [CONTRIBUTING.md](CONTRIBUTING.md).

| Component | In this tree | Source of truth | Licence |
|---|---|---|---|
| core-api (+ db schema) | `companions/core-api/` | here (was `smart-node-core`, archived) | root LICENSE (AGPL-3.0) |
| dispatcher / brin | `companions/brin/` | here (was `smart-node-brin`, archived) | root LICENSE (AGPL-3.0) |
| gateway | `companions/gateway/` | here (was `ai-gateway`, archived) | own `LICENSE`, MIT, AI Gateway Contributors |
| communication | `companions/communication/` | here (was `smart-node-communication`, archived) | root LICENSE (AGPL-3.0) |
| voice | `companions/voice/` | here (was `smart-node-voice`, archived) | root LICENSE (AGPL-3.0) |
| studio (end-user client) | `companions/studio/` | here (was `smart-node-office`, archived) | root LICENSE (AGPL-3.0) |
| admin-runner | `companions/admin-runner/` | here (was `smart-node-admin-runner`, archived) | root LICENSE (AGPL-3.0) |
| panel (operator UI) | `bootstrap/smart-node-ui/` | here (was `smart-node-core` `frontend/`, archived) | root LICENSE (AGPL-3.0) |
| auth (authum) | `bootstrap/authum/` | private upstream | root LICENSE (AGPL-3.0) |
| approvals adapter (hitl) | `companions/hitl/` | here | root LICENSE (AGPL-3.0) |
| approvals service (hitl server and caller) | `companions/hitl-upstream/` | private upstream | own `LICENSE`, Apache-2.0 |
| documents (doci) | `companions/doci/` | `github.com/dklymentiev/doci` | own `LICENSE`, MIT, Dmytro Klymentiev |
| memory (mesh) | `companions/mesh/` | `github.com/dklymentiev/mesh-memory` | own `LICENSE`, MIT, Dmytro Klymentiev |
| workflows (rein) | `companions/rein/` | `github.com/dklymentiev/rein-orchestrator` | own `LICENSE`, MIT, Dmytro Klymentiev |
| tracking (planner) | `companions/planner/` | private upstream | own `LICENSE`, MIT, Dmytro Klymentiev |

## Not carried here, and therefore not licensed by this repository

- **Runtime dependencies.** Python packages, npm packages, apt packages and
  Docker base images are fetched at install time and keep their own licences.
  They are not redistributed here; they are installed on the node. Inspect a
  node if you need its full dependency inventory.
- **The LLM.** A node needs a credential for a model provider (Anthropic today).
  The provider's terms are between the node's operator and that provider.
- **Customer demo content.** The gateway's archived repository also holds a
  tasting-room demo written for one customer. It is deliberately not here: it is
  not part of the product and has no business installing on every node. See
  `companions/gateway/VENDORED`.

## Provenance, stated rather than hidden

`tools/vendor-drift.sh` reports the state of every component: which are the source
of truth here, which are snapshots, and whether a snapshot has drifted from the
commit its marker names without saying so.

Known gaps at 2026-08-15, in the four that keep an upstream: `doci`, `mesh` and
`rein` carry no `VENDORED` marker at all, and `planner`'s has no `commit:` line,
so none of the four can be compared against what it claims to be.
