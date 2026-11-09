# Runbook: update a node

**Audience:** whoever operates the node. **Time:** 5 minutes for one companion,
20 to 40 for a full node.

> **Status.** Drilled on a throwaway node, 2026-08-02: a node built from the
> previous release was updated to current, one companion first and then the
> whole node, and every command below is the one that actually ran. The drill
> found nine things wrong with the earlier draft, including two commands that
> can never work and a whole-node update the installer refused outright.
>
> Not drilled: jumping more than one release at a time, and the uninstall
> at the end. Update a throwaway node first anyway, on the same path your
> production node will take.

## The model

There is no upgrade command and no migration runner. Every stage and companion
script is idempotent: running the new version of a script over an installed node
brings that piece to the new version and leaves the rest alone.

So an update is: push the newer script and its sidecars, run it, verify it,
reapply permissions, validate. The full installer does exactly this in order;
you can do it for one companion by hand.

Two consequences worth knowing before you start:

- **The database schema is baked, not migrated.** `B02` lays down an end state.
  A release that changes the schema will say so in its notes and will tell you
  what to do. If a release says nothing about the schema, assume the schema did
  not change, not that migration is automatic.
- **The permissions layer closes every path.** It runs at the end of a full
  install and of a single-component update alike, and it refuses paths nobody
  declared. A component that gained a directory in the new version and did not
  declare it will fail the update. That is the design working.

## Before anything

```sh
sudo /opt/smart-node/bin/sn-version            # what runs now
sudo /opt/smart-node/bin/sn-version check      # drift from the pin
```

Write the output down, and take a backup: [runbook-backup.md](runbook-backup.md).
An update you cannot reverse is a change you should not make on a Friday.

Read the release notes for everything between your version and the target. Do
not skip several releases in one jump on a node that matters; go one at a time
and validate between.

## Update one companion

The common case: a fix landed in the gateway, the voice companion, tracking.

You connect as the SSH user the node was installed with, normally `admin`, and
raise privilege with `sudo`. There is no root login: the installer's host
hardening sets `PermitRootLogin no`, so every `root@NODE` command you may have
seen elsewhere fails with `Permission denied (publickey)`.

From the controller, with the new checkout:

```sh
# 1. push the script and the sidecars it needs beside it
scp companions/tracking.sh admin@NODE:/tmp/
scp -r companions/planner companions/tracking companions/_shared admin@NODE:/tmp/

# 2. run it
ssh admin@NODE 'sudo bash /tmp/tracking.sh'

# 3. confirm it is healthy on its own terms
ssh admin@NODE 'sudo bash /tmp/tracking.sh --verify'
```

**A companion's sidecars are not named after the companion.** The tracking
companion carries `planner/` as well as `tracking/`; `memory.sh` carries `mesh/`;
`documents.sh` carries `doci/`. Copying only `companions/<name>/` by analogy
misses them, and the run dies on a missing file. The manifest lists them:

```sh
tools/sn-components sidecars tracking    # planner tracking _shared
```

Each companion supports `--verify` (idempotent post-check, exit 0 or the number
of failures) and `--status` (read its stage state file). Use them; do not judge
an update by the absence of red text during the run.

Then close it properly:

```sh
# permissions.sh is installed on the node, by B01, as a sidecar of the core
# stage. A node from a release older than that stage does not have it yet; the
# installer itself tests before running it, so test too rather than assume.
ssh admin@NODE 'sudo test -x /usr/local/lib/smart-node/permissions.sh \
                && sudo /usr/local/lib/smart-node/permissions.sh \
                || echo "no permissions.sh on this node yet (pre-dates the stage)"'

# validate.sh is NOT installed on the node and never has been. The installer
# copies it in, runs it, and deletes it. Do the same.
scp validate.sh admin@NODE:/tmp/validate.sh
ssh admin@NODE 'sudo bash /tmp/validate.sh; sudo rm -f /tmp/validate.sh'
```

Skipping the permissions step is the one shortcut that actually hurts: a
component re-run can create a directory, and the whole point of that step is
that no install path leaves an undeclared writable path behind.

**If the permissions run reports a group change, restart the affected services.**
A running process keeps the group list it started with. Adding a service account
to a group does not reach it until it restarts, so the account has the access and
the process does not. This has already happened once in the field: a gateway kept
its old groups, could no longer read the model credential, and every agent call
failed while the permissions themselves were correct. `validate.sh` will not
catch it, because on disk everything is right.

## Update the whole node

Re-run the installer against the same machine. It is the same sequence, in
dependency order, ending in validate:

```sh
orchestrator/install-on.sh \
  --provider existing \
  --name <same-name> --ip <ip> \
  --ssh-user <u> --ssh-key <key.pub> \
  --setup-via-token \
  --force \
  --all
```

Two flags there exist only because this is an update, and the installer refuses
without them:

- **`--force`.** The provisioner stops on a machine that already carries
  `/var/lib/smart-node/stages`, which every installed node does. The flag skips
  that one guard and destroys nothing. Do not confuse it with the lxd-local
  provisioner's `--recreate`, which DELETES the container.
- **`--setup-via-token`.** `--all` refuses to run without a first-login choice,
  a guard meant for fresh nodes. On a node that already has an owner this flag
  satisfies it and changes nothing. Do not reach for `--owner-password` here
  unless you actually intend to set that password.

Resources are a precondition, not an afterthought. The provisioner re-checks
them on every run and refuses below 2 GB RAM or 10 GB free on `/`. The node most
likely to fail that disk check is an old busy one, which is exactly the node you
are updating. What you copy to `/tmp` for a companion update counts against it
too, so clear it out when you are done.

Same `--name` as the original: it is the node FQDN and every routing rule
depends on it.

Re-supply the credentials the node already has (`--anthropic-key`,
`--gemini-key`, and so on) only if you are changing them. Generate-once files
are left alone on a re-run, so an omitted flag does not wipe a working
credential, but a flag with a wrong value will overwrite a good one.

Exit codes tell you where it stopped: 3 is a bootstrap stage, 5 is a Phase 2
companion, 6 means validate found failures after everything installed.

Read the message, not only the code, because the early refusals reuse them: a
bad or missing flag exits 4, a failed Layer P exits 1, and the missing
first-login guard exits 3 before a single stage has run. A code alone cannot
tell "stopped in bootstrap" from "never started".

## Images

None. No companion loads an image tarball any more: planner, mesh and
documents install natively, so an update builds nothing on the host first. A
node still running the doci containers is migrated by `documents.sh` itself.
`tools/versions.sh --check` still compares the versions stated in
`docs/install-from-source.md` with each companion's `STAGE_VERSION`.

## After

```sh
sudo /opt/smart-node/bin/sn-version
sudo /opt/smart-node/bin/sn-version check
```

Compare the component table against the one you wrote down before you started.
That comparison is the check: the components you meant to move should have
moved, and nothing else should have.

**`check` is not the thing that tells you the update happened.** It compares the
live composition against the lock, and the installer re-pins that lock at the
end of every run, so immediately afterwards the two agree and you get
`composition: UNCHANGED (matches the release)`. That is the lock doing its job,
not a verdict on your update. What the check is for is the time BETWEEN updates:
run it a week later and a `CHANGED` result means something altered the node
behind your back.

What does record the update is the release number and the lock's history. The
installer prints the result as it runs:

```
sn-version: bumped -> node release 1.0.1  fp b0d553a7291a
sn-version:     - planner: 0.3.0 -> 0.3.1
```

and the same lines land in the lock's `history` and as a
`node.composition.changed` event in `sn_core.events`, so the node can answer
"what changed, and when" long after you have closed the terminal:

```sh
sudo python3 -c "import json;print(json.load(open('/var/lib/smart-node/node-release.lock'))['history'][-1])"
```

One entry you may see says a version was resolved by the reader rather than
changed by the node. Upgrading `sn-version` itself can make a component that
previously read as `?` report a real version; that is the tool learning, not the
node moving, so it is written to the history without moving the release number.

Then look at the node the way a user would: send the assistant a message, open a
document, place a call if voice is installed. `validate.sh` checks that the parts
are alive, not that they still do the job.

## When an update fails

**A companion failed mid-run.** Re-run that one script. Companions are
idempotent, and a second run over a half-applied state is the intended repair,
not a risk.

**The permissions step refused a path.** Read the path it named. Either the new
version created a directory nobody declared, in which case it belongs in
`bootstrap/permissions.list` with a reason, or something on the node was
chmod'ed by hand and the list is correcting it. Do not widen the list to make
the message go away. A directory the node itself added (a flow's working
directory, say) is declared on the node, not in the release: one line in
`/etc/smart-node/permissions.local.list` (same format, root-owned, mode 0644).
Entries there must lie under `/var/lib/smart-node` or `/srv`, and a line for a
path the release already declares is ignored.

**Validate reports failures.** Read `/etc/smart-node/validation-report.md`. It
names the check, not just the count. A stage in `failed` state is the loudest
signal; artifact liveness failures usually mean a container did not come back
up, and `docker logs` on that container is the next step.

**Every container companion crash-loops on the database at once.** The logs say
`ConnectionRefusedError ... ('172.18.0.1', 5432)` or the equivalent for your
bridge address. The companions in containers reach PostgreSQL over the Docker
bridge, and PostgreSQL only listens there if the bridge existed when it started.
It does not fail when the address is missing: it logs
`could not bind IPv4 address` as a WARNING, comes up on `localhost` alone, and
reports success to systemd, so the cluster looks healthy while nothing can reach
it. Confirm with `ss -ltnp | grep 5432`: if only `127.0.0.1` is listed, that is
this. `systemctl restart postgresql@<ver>-smartnode` once Docker is up fixes the
running node.

From this release on the cluster is ordered after `docker.service`, so an
updated node does not race, and `validate.sh` now asserts that PostgreSQL is
actually listening on every address its own config declares rather than trusting
systemd's word for it. A node you have not updated yet still has the race, and
updating it is the fix.

**A companion container will not come up, and its log says
`permission denied for table <something you added>`.** Companions that own a
database grant across every table in their schema at startup, so a table you
created there yourself, under a different owner, stops the whole application
from booting. Keep your own tables out of `mesh`, `doci` and `planner`; put
scratch data in its own database.

**It is worse than before and you need out.** Restore:
[runbook-restore.md](runbook-restore.md). This is why step zero is a backup.

## Removing a component, and removing the node

A companion can be reversed on its own:

```sh
ssh admin@NODE 'sudo bash /tmp/gateway.sh --uninstall'
```

The whole node is reversed from the controller by replaying the artifact
manifest backwards. It keeps the host hardening in place, so the machine stays
in a defined state rather than an unknown one:

```sh
orchestrator/uninstall-from.sh --ip <ip> --ssh-user <u> --ssh-key <key>
```

This destroys the databases. It is not part of an update.

## Node-local settings survive an update

A release owns its configuration. The env files the installer generates
(`/etc/smart-node/*.env`, the `.env` files of the companions) are rewritten on
every update, and the trees under `/opt/smart-node/<companion>` are replaced
with `rsync --delete`. Never set a value by hand in one of them: the next update
removes it without a word.

Put it in the node layer instead: `/etc/smart-node/local/<unit>.env`, one file
per service, for example `/etc/smart-node/local/smart-node-runner.env`. Every
`smart-node-*.service` loads that file after its own, so a value there wins,
and the installer never writes, rewrites or deletes anything in
`/etc/smart-node/local` (`bootstrap/local-layer.sh`, run before the permissions
stage; `tests/local-config-layer.test.sh`). Keep the files root-owned, mode
0600. Restart the unit after a change:

    printf 'RUNNER_LEASE_SECONDS=3700\n' | sudo tee /etc/smart-node/local/smart-node-runner.env
    sudo chmod 600 /etc/smart-node/local/smart-node-runner.env
    sudo systemctl restart smart-node-runner

The same split holds for everything else a node adds: its own systemd
drop-ins under `<unit>.d/` with a name of its own (the release writes only
`zz-local.conf` there and leaves the rest), its own code in a directory of its
own, its own state under `/var/lib/smart-node`.
