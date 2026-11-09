# Runbook: back up a node

**Audience:** whoever operates the node. **Time:** 5 minutes to run, once you
have decided where the copy goes.

> **Status.** Smart Node ships no backup tool. This is the manual procedure.
> It was run end to end against a throwaway node on 2026-08-01, together with
> the restore in [runbook-restore.md](runbook-restore.md): a node was seeded with
> marker data, backed up, destroyed, reinstalled and restored. The commands below
> are the corrected ones. Do the drill yourself before you rely on it on a machine
> that matters, because your node is not the drill node.

## What actually has to survive

A node is three things: a Postgres cluster, a set of file trees, and a handful
of secrets that were generated once at install and exist nowhere else.

The secrets are the part people lose. Postgres roles were created with random
passwords written into env files. Restore the databases without those files and
every service comes up unable to authenticate. Restore the panel without
`/var/lib/smart-node/authum` and nobody can log in, including you: there is no
email reset and no recovery code, on purpose.

| What | Where | Lose it and |
|---|---|---|
| Databases `smartnode`, `mesh`, `planner`, `doci` | cluster `NN/smartnode`, data under `/opt/smart-node/postgres` | all node content is gone |
| Generate-once secrets | `/etc/smart-node/*.env`, `/opt/smart-node/*/*.env` | the dumps are unusable, services cannot authenticate |
| The node secret key | `/etc/smart-node/gateway-secret.key` | every secret in `sn_config.secrets` is unreadable forever, including by you |
| The age key, while `integrations.sops.yaml` still exists | `/etc/smart-node/age-key.txt` | a sops file not yet imported into the store cannot be imported |
| Agent runtime state | `/srv/agents/*/logs/`, `/srv/agents/*/state/` | agent definitions regenerate from the database; what they did does not |
| Passkeys and sessions | `/var/lib/smart-node/authum/` | nobody can log in; recovery needs shell access |
| Document store (git-backed) | `/opt/smart-node/documents/files/` | documents and their history are gone |
| Installer state and manifest | `/var/lib/smart-node/stages/`, `/var/lib/smart-node/manifest.jsonl` | the node no longer knows what it installed |
| Routing, TLS, node identity | `/etc/smart-node/` including `traefik/` | certificates and routes have to be reissued |
| Agent home and scratch | `/var/lib/smart-node/agent/`, `/opt/smart-node/scratch/` | agent working state is gone (usually acceptable) |
| Panel render data | `/srv/smart-node-hq/data/` | regenerated on the next panel run (skippable) |

The age key deserves its own line rather than being covered by "the `/etc/smart-node`
tree". It is the one file that makes everything else in that directory readable:
copy the directory and you have it by accident, copy only the `*.env` files and
you have lost the secret store permanently. There is no second copy anywhere.

Docker images are not state. They are loaded from the tarballs the installer
ships and can be reloaded. Do not back them up.

## Before the first backup: record the version

A backup is only restorable into a node running the same version. Capture the
fingerprint next to the copy:

```sh
sudo /opt/smart-node/bin/sn-version > /var/tmp/sn-version.txt
sudo cp /var/lib/smart-node/node-release.lock /var/tmp/
```

Both files go into the archive. Without them you will not know, six months from
now, which release the dump belongs to.

## The procedure

Run as root on the node.

### 1. Dump the databases

`pg_dumpall` in one shot keeps roles (and therefore the random passwords already
hashed into the cluster) together with the data:

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/var/tmp/sn-backup-$STAMP
mkdir -p "$OUT" && chmod 0700 "$OUT"

sudo -u postgres pg_dumpall --cluster "$(pg_lsclusters -h \
  | awk '$2 == "smartnode" {print $1"/"$2}')" \
  > "$OUT/cluster.sql"
```

Check it is not empty and contains all four databases:

```sh
grep -c "^CREATE DATABASE" "$OUT/cluster.sql"    # expect 4 or more
```

If the count is lower, stop. Something is wrong with the cluster selection, and
a partial dump that looks fine is worse than no dump.

### 2. Commit the document store

The store is a git repository, and uncommitted edits are not in its history.

Do this **inside the documents container**, not on the host. The store is owned
by the container's uid, and the directory above it is not traversable by that
uid on the host, so neither root nor the owner can run git there: as root git
refuses with `detected dubious ownership`, and as the owner it cannot even enter
the directory.

```sh
docker exec smart-node-doci sh -c '
  cd /var/www/html/files &&
  git add -A &&
  git diff --cached --quiet ||
  git -c user.name=smart-node -c user.email=node@localhost commit -qm "backup"'
```

Be aware of what this does and does not buy you: the drill showed the **files**
survive a restore but the **history does not**. The container re-seeds its store
on start and the restored refs did not survive that. Treat the git history as
convenience, not as a recovery guarantee, until that is fixed.

### 3. Archive the trees

```sh
tar -czf "$OUT/trees.tar.gz" \
    --numeric-owner --acls --xattrs \
    /etc/smart-node \
    /var/lib/smart-node \
    /opt/smart-node/documents/files \
    /opt/smart-node/*/*.env \
    /srv/tools 2>/dev/null

cp /var/tmp/sn-version.txt "$OUT/" 2>/dev/null || true
```

`--numeric-owner` matters: these trees carry the ownership the permissions layer
enforces, and restoring them with remapped names reopens exactly what
`permissions.list` is there to close.

### 4. Verify before you trust it

```sh
tar -tzf "$OUT/trees.tar.gz" | grep -c smart-node   # non-zero
tar -tzf "$OUT/trees.tar.gz" | grep authum          # passkeys present
ls -la "$OUT"
```

A backup you have not listed is a guess.

### 5. Move it off the node

The copy must not live on the machine it protects. Pull it from the controller,
never push credentials to the node:

The installer hardens the host and **disables root login over SSH**, and the
backup directory is `0700 root`, so an `scp root@NODE:` or an `scp` as the
ordinary SSH user both fail. Stream it through sudo instead:

```sh
# on the controller
ssh <user>@NODE "sudo -n tar -cf - -C /var/tmp sn-backup-STAMP" > backup.tar
ssh <user>@NODE "sudo -n shred -u /var/tmp/sn-backup-STAMP/cluster.sql; \
                 sudo -n rm -rf /var/tmp/sn-backup-STAMP"
```

Then treat it as what it is: the archive contains every credential the node
holds, including model provider keys and mailbox passwords. Encrypt it at rest.
Anyone who reads it owns the node.

## Automating it

A cron entry that runs steps 1 to 4 nightly and a pull from the controller is
enough. Two rules:

- Keep at least seven daily copies plus one monthly. A single overwritten copy
  protects you against disk failure and against nothing else, and the failure
  you will actually hit is a bad change discovered three days later.
- Alert on the absence of a fresh copy, not on backup errors. A backup job that
  stopped running silently is the normal way people discover they have no
  backups.

## What this does not cover

- **The model provider.** Conversations held by the provider are not on the node
  and are not in this archive.
- **External mailboxes.** If the node reads mail from an external host, that
  host has its own backup problem.
- **The host itself.** This restores a node onto a machine the installer has
  already prepared. It is not a bare-metal image.
