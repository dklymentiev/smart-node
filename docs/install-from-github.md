# Installing a node from a published release

**Audience:** whoever puts a node on a machine. **Time:** allow 30 to 45 minutes.

This is the ordinary path. You do not need a build host, a checkout of anything
else, or access to how the project is developed. You need one release.

If you want to build every component from source instead, that is
[install-from-source.md](install-from-source.md), and it is the longer road.

## What a release is made of

The repository at a release tag contains the installer and the vendored source
of every component, and that is the whole release: no component ships an
image, so every one installs natively from that source on the target. This
needs internet access on the target, but no private build host and no other
repository.

## One command

```sh
git clone https://github.com/dklymentiev/smart-node.git && cd smart-node

tools/install-from-release.sh -- \
    --provider lxd-local --name node1 --ip <node-ip> \
    --ssh-key ~/.ssh/id_ed25519.pub \
    --node-fqdn node1.localhost --node-domain node1.localhost --no-vpn \
    --setup-via-token --all
```

`--node-domain` gives Studio its address. Without it the install ends with an
error after every check has passed. For a node you use from this machine,
`node1.localhost` is enough and Studio is `http://studio.node1.localhost/`; see
[A node on your own computer](install-from-source.md#a-node-on-your-own-computer)
for the browser door, and [public-domain.md](public-domain.md) for a real domain.

It reads the newest release, makes a clean clone at that tag, and hands the
result to the installer. Everything
after `--` goes to
[`orchestrator/install-on.sh`](../orchestrator/install-on.sh) unchanged, so every
flag in its `--help` works here.

In an interactive terminal, `--all` pauses at `claude setup-token` for you to
authorize your own Claude account. It will not reach final validation while the
prompt is unanswered. Complete it, or press `Ctrl-C` to leave Claude deferred
and continue; connect it later in **Studio -> AI Gateway -> Connect**. Headless
runs defer the prompt automatically.

Useful on their own:

```sh
tools/install-from-release.sh --prepare-only      # fetch and verify, install nothing
tools/install-from-release.sh --tag v1.10.4 -- …  # a specific release
tools/install-from-release.sh --workdir /opt/sn   # keep the tree rather than a temp dir
```

## Private-repository testing

Pass a token, and nothing else changes:

```sh
tools/install-from-release.sh --token ghp_… -- --provider lxd-local …
# or: export GITHUB_TOKEN=ghp_…
```

The token is used for the clone and the release lookup and is **not** left behind: the
working copy's remote is rewritten to the plain URL afterwards, so a tree left on
an operator's machine does not carry a credential.

A token with read access to the repository is enough. It needs nothing else.

## What it refuses to do, and why

- **The tag is not a published release.** It stops before cloning.
- **The requested work directory is not empty.** It stops rather than overwrite
  an existing checkout.

## After it finishes

The installer prints where the node is and how to reach it. Read the validation
report, then [docs/README.md](README.md) under "First hour": an installed node
runs and does nothing until something has been asked of it.

```sh
ssh admin@<ip> 'sudo cat /etc/smart-node/validation-report.md'
```

To put the node on a domain the world can reach, see
[public-domain.md](public-domain.md). To update it later,
[runbook-update.md](runbook-update.md).
