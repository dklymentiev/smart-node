# Runbook: restore a node

**Audience:** whoever operates the node, on the worst day. **Time:** 30 to 60
minutes, most of it the reinstall.

> **Status.** Run end to end against a throwaway node on 2026-08-01: seeded with
> marker data, backed up, destroyed, reinstalled, restored, verified. The
> commands below are the corrected ones; the first draft failed at three separate
> steps, two of them silently. Run it yourself once before you need it. An
> untested restore is not a restore, and this one is only tested on one node.

## The model

Smart Node is an appliance that owns its machine. You do not repair a broken
node in place. You stand up a clean one at the same version and pour the data
back in.

That is the whole procedure:

1. Confirm what the backup is.
2. Install a clean node at the same version.
3. Stop the services.
4. Restore databases, secrets and trees.
5. Reapply permissions, start, validate.

## Step 0. Confirm what you are holding

```sh
cat sn-backup-STAMP/sn-version.txt
grep -c "^CREATE DATABASE" sn-backup-STAMP/cluster.sql   # expect 4 or more
tar -tzf sn-backup-STAMP/trees.tar.gz | grep -E 'authum|/etc/smart-node/'
```

Restore into the same version the backup came from. A newer node will have
schema the dump does not carry; an older one will not understand the dump. If
the versions differ, install the version in `sn-version.txt` first, restore,
then follow [runbook-update.md](runbook-update.md) to move forward one step at a
time.

## Step 1. Install a clean node

From the controller, against the replacement machine:

```sh
orchestrator/install-on.sh \
  --provider existing \
  --name <same-name> --ip <new-ip> \
  --ssh-user <u> --ssh-key <key.pub> \
  --owner-password <temporary> \
  --all
```

Use the same `--name`: it becomes the node FQDN and every routing rule depends
on it. The password you pass here is thrown away in step 4 when the real
identity store comes back.

Do not skip `--all` in the belief that the restore will supply the missing
pieces. It will not: the installer creates users, groups, systemd units, the
Docker network and the cluster, and the archive contains none of those.

## Step 2. Stop everything that writes

```sh
sudo docker ps --format '{{.Names}}' | grep '^smart-node' \
  | xargs -r sudo docker stop
sudo systemctl stop 'smart-node-*'
```

Leave Postgres running. It is the one thing the next step needs.

## Step 3. Restore the databases

The fresh install has already created the four databases **and their schema**.
`pg_dumpall` output cannot be poured on top of that: every `CREATE` collides,
the errors scroll past, and the data is never loaded. You have to drop the four
databases first. Services are stopped, so nothing is using them.

```sh
CLUSTER=$(pg_lsclusters -h | awk '$2 == "smartnode" {print $1"/"$2}')

for db in smartnode mesh planner doci; do
  sudo -u postgres psql --cluster "$CLUSTER" -qc "DROP DATABASE IF EXISTS \"$db\";"
done

cat /var/tmp/sn-backup-STAMP/cluster.sql | sudo -u postgres psql --cluster "$CLUSTER"
```

Pipe the file in rather than passing `-f`: the backup directory is `0700 root`
and the postgres user cannot read it, which fails with a single
`Permission denied` line that is easy to miss.

Expect noise about roles that already exist. That is harmless: roles are
cluster-wide and the fresh install made them.

**Check the content, not the database list.** Counting four databases proves
nothing here, because the fresh install created four databases; that check
passes just as happily when the restore did nothing at all. Look for something
only the backup could have:

```sh
sudo -u postgres psql -d smartnode -tAc \
  "SELECT count(*) FROM sn_config.agents;"        # more than the one seeded agent
sudo -u postgres psql -d smartnode -tAc \
  "SELECT count(*) FROM sn_core.messages;"        # your traffic, not zero
```

## Step 4. Restore secrets and trees

This is the step that decides whether the node works.

```sh
sudo systemctl stop postgresql@${CLUSTER%%/*}-smartnode
sudo tar -xzf /var/tmp/sn-backup-STAMP/trees.tar.gz \
     -C / --numeric-owner --acls --xattrs
sudo systemctl start postgresql@${CLUSTER%%/*}-smartnode
```

Postgres is stopped for the extraction because the archive covers
`/var/lib/smart-node` and you do not want a running service reading a tree that
is half-written.

What just came back, and why each one matters:

- `/etc/smart-node/*.env` and `/opt/smart-node/*/*.env`: the generate-once
  passwords. Without them the services cannot reach the databases you restored
  in step 3.
- `/var/lib/smart-node/authum/`: passkeys and sessions. This is what makes the
  temporary password from step 1 irrelevant and lets the real owner log in.
- `/var/lib/smart-node/manifest.jsonl` and `stages/`: what the node believes it
  installed. `validate.sh` reads these.
- `/opt/smart-node/documents/files/`: the document store, with its git history.

## Step 5. Reapply permissions, then start

Never start the services before this. The archive restored ownership, but a
mismatch between the tree and `permissions.list` is exactly the condition the
permissions layer exists to catch:

```sh
sudo /usr/local/lib/smart-node/permissions.sh --verify   # look first
sudo /usr/local/lib/smart-node/permissions.sh            # then apply
```

If it refuses a path, it found something writable that nobody declared. Do not
chmod around it. Read what it named: either the archive carried a tree from an
older layout, or the extraction remapped an owner.

Then:

```sh
# A glob does NOT work here: systemd expands it over LOADED units only, and a
# stopped unit is not loaded, so `systemctl start 'smart-node-*'` starts nothing
# and says so in a hint most people scroll past. Enumerate them, and do not
# forget the timers: starting only the services leaves validate failing on a
# timer that never came back.
units=$(systemctl list-unit-files 'smart-node-*' --no-legend \
        | awk '{print $1}' | grep -v '@')
sudo systemctl start $units

sudo docker start $(sudo docker ps -aq --filter 'name=^smart-node')
sudo /usr/local/lib/smart-node/validate.sh
```

One oneshot may fail on the first start because it ran before core-api was
ready. Start it again by name and confirm it reports success before you treat it
as a real failure.

## Step 6. Prove it, do not assume it

`validate.sh` exiting 0 means the machinery is alive. It does not mean your data
came back. Check the four things a person would notice:

- Log in to the panel with an existing passkey, not the temporary password.
- Open a document that existed before the failure.
- Search memory for something old.
- Confirm the task board still has its history.

Only then change the owner password and retire the temporary one.

## If the node had a public domain

Certificates live under `/etc/smart-node/traefik/` and came back with the
archive, but they are issued against a name that now points at a different IP.
Update DNS to the new address before expecting TLS to work, and give the ACME
client time to settle. See [public-domain.md](public-domain.md).

## When restore is the wrong tool

If the loss is one component rather than the machine, do not restore the whole
node. Re-run that companion's installer: it is idempotent and rebuilds its own
container, service and config without touching anyone else's data. See
[runbook-update.md](runbook-update.md).

Restore is for a dead machine, a corrupted cluster, or a change you cannot
reverse.
