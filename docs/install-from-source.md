# Smart Node: install from source

Runbook for an operator putting a Smart Node on a server. There are no prebuilt
image tarballs to download and nothing to fetch from a build host: everything
installs from this one source repository.

Do exactly these steps. Do not invent steps. Where a version or path is needed,
**read it from the companion script named below**. That is the source of truth,
never guess.

## What you are doing (one sentence)

Clone 1 repo -> run `install-on.sh` (it installs every component from the
vendored source) -> verify -> hand over.

## Prerequisites (on the client's host)

- A Debian or Ubuntu controller with `git`, `ssh`, `jq`, and Docker. For
  `lxd-local`, install LXD and initialise its `default` storage pool and `lxdbr0`
  network first. An `existing` target needs Debian or Ubuntu, systemd, `apt`,
  passwordless sudo for the SSH user, 2 GB RAM, and 10 GB free on `/`.
- Internet access. The target fetches operating-system packages, pip
  dependencies and model-provider tooling.
- An SSH keypair (public + private, same path minus `.pub`).
- A free node IP for LXD.
- A supported model credential for useful answers: Anthropic or OpenRouter can
  be supplied to the installer, and Anthropic, Google, or OpenRouter can be
  connected after installation. The node installs without one but agents cannot
  answer usefully until a provider is connected.

## Step 0: preflight (recommended)

Before anything else, check the host/target is ready and get exact fix commands
for anything that is not:

```sh
smart-node/tools/preflight.sh --provider lxd-local          # for a local LXC
smart-node/tools/preflight.sh --provider existing --ip <ip> --ssh-user <u> --ssh-key <key.pub>
```

It catches the common blockers: missing LXD, absent storage pool or bridge,
Docker's FORWARD policy silently dropping container traffic, insufficient RAM or
disk. Add `--fix` to apply the safe, idempotent fixes automatically.

## Step 1: clone

```
git clone https://github.com/dklymentiev/smart-node.git
```

Everything the node runs is in `smart-node` (vendored source of core-api, gateway,
Studio, brin, doci, mesh, communication, voice, planner, all tracked in git).
Nothing else has to be cloned: this repo is the whole input.

## Step 2: nothing to build

No component ships an image you have to build or load. Planner, mesh and
documents (doci and its MCP sidecar) are native systemd services, installed on
the target from the vendored source by their companion scripts. Go straight to
Step 3.

`tools/versions.sh` prints each companion's `STAGE_VERSION`, and
`tools/versions.sh --check` fails if this document drifts from the scripts.
Current value at time of writing (CONFIRM against `companions/documents.sh`):
doci `0.3.6`.

## Step 3: install

```sh
sudo smart-node/orchestrator/install-on.sh \
  --provider lxd-local --name <node> --ip <ip> \
  --ssh-key <path/to/id.pub> --all \
  --setup-via-token \
  [--node-domain <domain> --acme-email <email>]   # real Let's Encrypt cert
```

`--setup-via-token` avoids putting a password in shell history. The one-time
token is written to `/etc/smart-node/install-report.md` and drives the panel's
first-run form. For unattended automation, `--owner-password` seeds the initial
`admin` login directly; supply it through an appropriately protected execution
environment.

It provisions the LXC, ships the companions in, loads the images you built, runs
the native companions from source, and validates. ~30 min headless.

When the command has an interactive terminal, the agent-runtime stage launches
`claude setup-token` and waits for you to authorize your own Claude account.
Until you answer that prompt the remaining companions and final validation do
not run. To install without authorizing Claude yet, cancel that prompt with
`Ctrl-C`; the installer records Claude as deferred, continues, and leaves the
later connection at **Studio -> AI Gateway -> Connect**. A headless run defers
this step automatically.

### Give the node a channel

The node needs at least one way to receive and send messages, or the
communication component is skipped and the node can only be talked to through
the panel. Any one channel is enough; none is privileged over another.

**Email against a mailserver you already run.** This is the common case: the node
does not host mail, it reads mailboxes off the host that holds the valid PTR,
certificate and DKIM for the domain.

```sh
  --mail-host <host-or-ip>       # IMAP/SMTP host, e.g. the bridge address
  --mail-account <address>       # a single mailbox, and
  --mail-password <password>     #   its password
  # or, for several mailboxes at once:
  --mail-accounts-file <path>    # lines of PW_<addr>=<password>, where the key
                                 # is the address with @ and . replaced by _
                                 #   PW_hello_example_com=...
```

Ports default to 993 (IMAP) and 587 (SMTP). Certificate verification defaults to
**off**, because an external mail host reached over a private address presents a
certificate for its public name, not for the address you dialled. Override with
`SMART_NODE_MAIL_IMAP_PORT`, `SMART_NODE_MAIL_SMTP_PORT` and
`SMART_NODE_MAIL_TLS_VERIFY` if your deployment dials the certificate's own name.

**Telegram**: `--tg-token` + `--tg-channel-id` (a per-node bot from BotFather).

After the install, bind each address to an agent in the panel. The transport is
agent-agnostic on purpose, because which address an agent answers is not an
installer decision, so a node with a mail channel and no binding polls nothing
and says so in its log.

## Step 4: verify

- Wait for the line `Validation PASS -- N/N checks passed (0 failed)`. The check
  total grows as companions are added (more than 200 today); what matters is
  `0 failed`.
- Smoke test the human path from the controller (substitute your `--node-fqdn`,
  or the existing hostname if `--node-fqdn` was not given, and the node IP):
  ```sh
  curl -sk -o /dev/null -w '%{http_code}
' -H "Host: auth.<fqdn>"  https://<node-ip>/   # expect 200
  curl -sk -o /dev/null -w '%{http_code}
' -H "Host: panel.<fqdn>" https://<node-ip>/   # expect 302 -> auth
  ```
  A 200 on the auth host with the login page, and panel redirecting to it, means
  the node is reachable and the operator can log in and reach `/setup`.
- Confirm the node's permission surface matches what is declared. `validate.sh`
  already asserts it, but it is worth knowing the command, because this is what
  you run after any later change:
  ```sh
  ssh <node> 'sudo /usr/local/lib/smart-node/permissions.sh --verify'
  # expect: drift=0 undeclared=0
  ```
  `--verify` only reports. Running it without the flag applies the list. See
  SECURITY.md for what it enforces and why an undeclared writable path is a
  refusal rather than a warning.

## Step 5: hand over

Give the client the node URL + operator login. The client logs in and talks to
**John** (the configurator). John interviews them in a LIVE chat, in business
language, and sets the business up. Nothing else to configure by hand.

## Step 6: give the node a real domain (optional, manual)

The node now answers on its internal name. Putting it on a domain the outside
world can reach is a separate, manual procedure of five places, four of them
inside the node. One of them is easy to miss and leaves a node that looks
completely healthy until someone presses Logout.

See [public-domain.md](public-domain.md). Do not improvise it from memory.

## A node on your own computer

For a node only you use, from the machine it runs on, name it under `localhost`.
Browsers send every `*.localhost` name to the machine itself, and the node then
serves plain http, so there is no certificate to accept:

```sh
orchestrator/install-on.sh --provider lxd-local --name mynode \
  --ip 10.22.207.50 --subnet 10.22.207.0/24 --ssh-key ~/.ssh/id_ed25519.pub \
  --node-fqdn mynode.localhost --node-domain mynode.localhost --no-vpn \
  --setup-via-token --all
```

Use your LXD bridge's subnet for `--ip` and `--subnet` (`lxc network get lxdbr0
ipv4.address`). A local node's proxy listens on the node's own loopback, so give
the container a door to it from the host:

```sh
lxc config device add mynode web80 proxy listen=tcp:0.0.0.0:80 connect=tcp:127.0.0.1:80
```

Studio is then `http://studio.mynode.localhost/`.

**On Windows, in WSL (Ubuntu with systemd).** Two settings the container needs
there, best set right after the installer creates it:

- `lxc config set mynode security.privileged true` and `lxc restart mynode`:
  the WSL kernel does not let Docker mount its layers inside an unprivileged
  container, and the install stops at the routing step with "failed to extract
  layer ... invalid argument".
- `lxc config set mynode boot.autostart true`: WSL stops an idle distribution,
  and the container with it.

If an install stops part way, the container stays. Run the installer again with
`--provider existing --force --ssh-user admin` and the same `--ip`, `--ssh-key`
and node options; `--provider lxd-local` would only refuse, because the
container exists.

## Guardrails

- Build images ON THE HOST, never inside the LXC.
- Versions + tarball paths come from `STAGE_VERSION` / `IMAGE_TAR` in the companion
  scripts. They are the source of truth. Do not invent versions.
- Build EVERY image a loader expects. If a script references one you did not build
  (doci-mcp is the one people miss; `documents.sh` loads it every run), build it before install.
- Do NOT hand-edit vendored files, `grants.json`, `agent.yaml`, `.mcp.json`, or
  `/etc/cron.d`. Those are projections, and editing them does nothing. All node state
  changes go through `install-on.sh` and the core-api endpoints.
- If an image build fails, the fix is in that component's Dockerfile or its
  `*-image/README.md`, not in inventing a workaround.
- If a step is unclear, read the companion script; do not improvise.

---

*This runbook describes the current one-repository path. `install-on.sh` is the
single install entry point; the exact clean-room result is recorded in the
release-readiness report rather than frozen here as a check count that will
immediately drift.*
