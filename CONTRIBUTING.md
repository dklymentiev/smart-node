# Working with the Smart Node source

Smart Node is open source under AGPL-3.0, but this upstream repository is
maintainer-published and does not accept outside pull or merge requests. Forks
are welcome: use this guide to build, test, and maintain changes in your fork.

## Prerequisites

- An Ubuntu host with `docker`, `lxd` (run `lxd init` once), and `git`.
- Internet access on the target node (apt, pip, npm and the mesh model).

## Component images

There are none to build. Every companion installs from the vendored source in
`companions/` on the node itself; documents (doci and its MCP sidecar) was the
last one shipped as an image.

## Version drift guard

Versions live in the companion scripts (`STAGE_VERSION`).
`tools/versions.sh` prints them, and:

```sh
tools/versions.sh --check
```

fails if the docs or anything else drift from the scripts. Run it before you
commit or release a change that touches versions.

## Where to commit a change to a component

`companions/<name>/` holds a component's source. For most components **that is
the source**: edit the file, commit it here, done. Those components had separate
repositories until 2026-08-15, when they were archived, because two places for one
program is a trap -- for a month every change landed here while the other place
stood still and still looked primary. Each carries a `VENDORED` marker naming the
archived repository, so the old history stays findable; archived means read-only,
not deleted. See [NOTICE.md](NOTICE.md) for the full list.

`tools/vendor-drift.sh` prints which components are the source of truth here and
which are snapshots.

## Never re-vendor a snapshot wholesale

This applies to the four components that still have an upstream of their own --
documents (doci), memory (mesh), workflows (rein) and tracking (planner). For
them `companions/<name>/` is a pinned snapshot, and `<name>.sh` syncs it onto the
node with `rsync -a --delete`. The obvious maintenance action, refreshing the
snapshot before a release, is the dangerous one, and the rule below exists because
it has already cost us twice.

**Drift has a direction, and it is not the same for every component.** Measured
2026-08-14, when all seven of the now-archived ones diverged as well -- kept here
because it is what the rule is made of:

| companion | vendored | upstream | paths |
|---|---|---|---|
| voice | 1.0.0 | 1.0.0 | 10 |
| brin | 1.6.2 | 1.5.1 | 27 |
| core-api | 0.18.0 | | 34 |
| gateway | 2.6.0 | 2.5.1 | 16 |
| communication | 1.8.0 | 1.7.0 | 11 |
| admin-runner | | | 13 |
| Studio | 0.5.0 | 0.5.1 | 9 |

Four are AHEAD, so a refresh reverts shipped fixes. `voice` carries the same
version number as upstream while ten files differ, so nothing detects it by
looking. `Studio` is ahead in some files and behind in others, so neither
direction is lossless. `rsync --delete` reports none of this: no conflict, no
warning, and the tests pass afterwards because reverted code is older code that
used to work. The regression surfaces later as behaviour that "came back".

So:

1. **Diff before you sync, and read it.** `tools/vendor-drift.sh` does it for
   every snapshot and fails on anything the `VENDORED` marker does not declare.
   For a component whose source is this tree it says so and compares nothing,
   because there is nothing to compare against.
2. **Ship one file when one file changed**, after proving that file was
   byte-identical to upstream beforehand.
3. **Never trust a `VENDORED` marker's claim about itself.** authum's said all
   four of its files were verbatim while the Dockerfile had a deliberate patch;
   admin-runner's says "no tests" with `tests/` sitting next to it. Verify with
   a diff, not by reading the marker.
4. **Declare what stays ahead.** A `drift: <path> -- <why>` line in `VENDORED`
   is a claim a human made on purpose; anything undeclared is what the checker
   is for.

And after any vendor and bake, prove the three copies agree -- `git show
<tag>:<file>` == the vendored file == what the image was built from. A tag is
only true if it names what shipped.

## Run an install

Point the single entry point at a target and let it run:

```sh
orchestrator/install-on.sh --provider lxd-local \
  --name <name> --ip <ip> --ssh-key <key.pub> --all
```

See `orchestrator/install-on.sh --help` for the full flag list and
[docs/install-from-source.md](docs/install-from-source.md) for the end-to-end
runbook.

## Run the tests

`tests/run-source.sh` runs every `tests/*.test.sh` against the working tree; it
needs no node. It does need a Linux host (WSL works) with Python 3.12, Node 20 or
later, `git`, `jq` and PostgreSQL's binaries (`initdb`; Debian/Ubuntu package
`postgresql`), plus Python environments that stand in for the node's:

```sh
python3 -m venv ~/.venvs/smart-node-pytest       # found by run-source.sh itself
~/.venvs/smart-node-pytest/bin/pip install -r companions/core-api/requirements-dev.txt \
  -r companions/gateway/requirements.txt -r companions/communication/requirements.txt \
  -r companions/admin-runner/requirements.txt -r companions/voice/backend/requirements.txt \
  requests
~/.venvs/smart-node-pytest/bin/pip install -e companions/rein
python3 -m venv ~/.venvs/sn-brin                 # brin needs a newer pydantic than core-api
~/.venvs/sn-brin/bin/pip install -r companions/brin/requirements.txt httpx

P=~/.venvs/smart-node-pytest/bin/python B=~/.venvs/sn-brin/bin/python
export CORE_API_PY=$P COMM_PY=$P GATEWAY_PY=$P SN_FLOW_PY=$P SN_REIN_PY=$P \
       SN_REIN_CWD=$PWD/companions/rein BRIN_PY=$B SN_BRIN_PY=$B SN_DISPATCHER_PY=$B
tests/run-source.sh
```

Without those variables a test looks for the component's venv where a node keeps
it (`/opt/smart-node/<component>/venv`) and reports `CANNOT RUN here` when it is
not there. `permissions-sn-op.test.sh` stages some fixtures under `/work`; from a
checkout elsewhere those cases report `CANNOT RUN` too. A run with no failures but
some tests that could not run exits 2, not 0. The tests leave scratch files (`.tmp_*`,
`__pycache__`) behind; run them in a clone you can throw away.

Studio's own tests: `cd companions/studio && npm ci && npm test`. The
`*.e2e.test.mjs` files need a running node and fail without one.

## If your component creates a directory, declare it

`bootstrap/permissions.list` is the single place where a node's permissions are
decided. A companion creates directories; it does not choose their modes. The
permissions stage runs at the close of every install and **refuses** any path
that is writable beyond its owner and is not declared in that list. A refusal
fails the install rather than blessing something nobody reviewed.

So if your component creates a directory or file that anyone other than its
owner can write:

1. Add a line to `bootstrap/permissions.list` giving path, owner, group, mode,
   and a comment saying why that access is needed. The reason is the point: a
   list of modes without reasons decays into noise nobody can audit.
2. Prefer no group-write at all. A component that owns its tree writes it as its
   own user; group-write is for state genuinely shared with another account.
3. If a whole subtree must stay closed, use the `tree` flag instead of listing
   files one by one.

Do not work around a refusal by widening the list without a reason, and do not
`chmod` on a running node, because the next install puts it back. See
[SECURITY.md](SECURITY.md) for the model this enforces.

## Companion scripts must implement --contracts-only

Every companion script must support a `--contracts-only` argument for differential companion updates. The orchestrator calls `bash NAME.sh
--contracts-only` on an update when the companion's `STAGE_VERSION` has not
changed since the last run. This mode must write only the node's contracts:
systemd unit files, env files, MCP shim files (`/etc/smart-node/mcp.d/`),
integration records in `sn_config.integrations`, permission grants (sudoers),
and cron entries. It must skip the slow install steps: venv creation, pip
install, docker pull, schema migrations, and copying application files to
`/opt`. A comment in each companion script must name the functions that belong to each category.

A script that does not recognise `--contracts-only` will exit non-zero (via the
`die "unknown arg: $1"` catch-all in the arg-parse case), which is the correct
behaviour -- the orchestrator treats non-zero as a fatal failure and stops the
update. To prevent this, every new companion must recognise the flag and
implement the mode. Routing to `run_intent` silently when the flag is unrecognised
is not acceptable.

## Companion scripts must not store bridge address literals

Any companion script that writes a service endpoint into an env file, systemd
unit, integration record, or any other installed artifact must use one of exactly
two values for the host part:

- `127.0.0.1` for a native caller (a systemd service, an agent, a script, or a
  tool that runs directly on the host).
- `host.docker.internal` for a container caller (anything that runs inside Docker
  and needs to reach a host-native service).

No `172.x.x.x` address is written by a companion. A service that must be
reachable by both native and container callers binds `0.0.0.0`; access is
controlled by the `open_firewall()` call that accompanies every such service, not
by the bind address. `open_firewall()` must open the port only to the docker
bridge subnets, never to `Anywhere`.

Document the caller type in a comment next to each endpoint assignment.
Increment `STAGE_VERSION` whenever the bind address or `open_firewall()`
calls in a companion change, so the differential update triggers a full
re-install on the next run.

## Companion scripts must read node domain from routing.env

Any companion script that writes the node's domain name into an artifact (a Traefik
route file, a systemd unit, a database row, or any other installed file) must derive
that name by sourcing `/etc/smart-node/routing.env` and reading `SMART_NODE_DOMAIN`.

```sh
source /etc/smart-node/routing.env
if [[ -z "${SMART_NODE_DOMAIN:-}" ]]; then
    echo "ERROR: SMART_NODE_DOMAIN is not set in routing.env. Cannot write node identity." >&2
    exit 1
fi
```

No call to `hostname(1)`, `hostname -f`, `dnsdomainname`, or any other OS-derived
identity lookup is permitted. An OS call that happens to return the correct value on
the day it was written is a guess, and a guess is wrong when the node is moved,
renamed, or cloned.

If `SMART_NODE_DOMAIN` is empty after sourcing `routing.env`, the companion must fail
with an explicit error. No fallback. An empty variable is a configuration defect, not
a reason to guess.

Increment `STAGE_VERSION` whenever domain derivation changes in a companion so
the differential update re-runs that companion and rewrites any stale domain
literal in a companion-owned artifact. Explain the invariant in a comment next
to the sourcing block.

## Forking and local changes

- Fork the repository and make changes in your own fork. The upstream
  maintainers do not accept outside pull or merge requests.
- Branch from your fork's `main` with a descriptive prefix, e.g. `feat/...`,
  `fix/...`, `chore/...`, or `docs/...`.
- Use Conventional Commits for messages: `type(scope): summary`
  (`feat`, `fix`, `chore`, `docs`, `refactor`, `test`). Keep the summary in the
  imperative mood.
- Keep shell scripts `bash -n` clean and prefer small, focused commits.

## What travels with a change

A change is not finished when it works; it is finished when something else would notice if
it stopped working.

**A fix carries a test.** If it needs one process, put a unit test beside that
service in `companions/<name>/tests/`; if it needs two or more live services or
the live database, use the system suite. Write the test so that it fails before
the change and passes after; if it passes both ways it is describing the code
rather than checking it.

**Where a test genuinely cannot reach**, an installer's shell being the usual case, carry a
REPRODUCTION instead: a few lines a reader can run that show the wrong behaviour and then
the right one. A three-line demonstration in a commit or release note is worth more than a
paragraph asserting that it was checked.

**Do not write a test that mirrors the implementation.** A check that greps source text, or
asserts the shape of a function rather than its result, fails on a renamed variable and
passes on a real regression. Assert what the code DOES.

**Nothing here asks for coverage.** A number that has not moved in a year guarantees
nothing. What matters is that the specific thing that broke is now watched.

## What must not enter the repository

These rules apply to changes in every fork and to any source you redistribute:

- **No credentials, ever** -- not in code, not in a test fixture, not in a comment showing
  "what the value looks like". A test needs a shape, not a real token.
- **No personal data.** Names, addresses, chat ids and mailboxes belong in the node's own
  registries at runtime, never in the source. `sn_config.user_channels` is the only place
  addresses live; a hardcoded one in a prompt or a script is a defect twice over.
- **No private hostnames or internal tracker references** in product text.
- **No deployment-specific literals**: machine-layout root paths, container bridge addresses,
  and domain names are facts about this installation, not about the product. Use environment
  variables instead.

**Everything committed is written in English** -- code, comments, commit messages, and
documentation. This is a product other people install and
read. Conversation with the owner happens wherever it happens; the repository does not.

## Documenting a defect fix in a fork

Describe the DEFECT before the change. A reviewer who has to reconstruct the problem from
the diff is being asked to do the diagnosis again, and will usually agree with whatever the
diff implies.

State four things:

- **the symptom**, as an operator meets it, with the exact command and verbatim output;
- **the cause**, in the code;
- **the discriminator** -- the observation that ruled out the other explanations. A 404 that
  might mean "absent" or might mean "refused" is not evidence until something separates
  them;
- **what you deliberately did not touch**, and why. Keep adjacent defects in
  their own commits so they are not lost in an unrelated change.

Say plainly how the change was verified, including what you could NOT verify. "pytest is not
installed on this machine, so the assertions were run directly with the service interpreter"
is a useful sentence; silence in its place is not.

For reliable defect work, keep claim, reproduction, independent confirmation,
fix, review, and release as distinct steps. The party who found a defect should
not be the only party to confirm it.

## Release numbering

`RELEASE` at the repo root is the node release, semver, and each release is
tagged `vX.Y.Z`.

- **patch** (`1.8.0` -> `1.8.1`) -- a fix, a wording change, a cleanup. Nothing
  an operator has to learn. This is the normal case and most releases are this.
- **minor** (`1.8.1` -> `1.9.0`) -- a node can do something it could not do
  before: a new companion, a new channel, a new capability an operator would
  notice.
- **major** -- a node cannot be updated in place, or an interface an operator or
  an agent depends on changes shape.

When in doubt, patch. A minor spent on a typo is a minor that cannot be spent on
the feature it should have marked.

## Reporting security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
