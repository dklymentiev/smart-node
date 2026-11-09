# sn-version -- what is installed on this node, and what changed since

## Two numbers, two questions

The canon release and node counter answer different questions. They are named apart in the tool's
output and must stay named apart everywhere else.

**The canon release** -- `/etc/smart-node/release`

    release=1.10.6
    commit=59ce91d
    installed_at=2026-08-15T11:00:00-05:00

Which release built this node. `orchestrator/install-on.sh` writes it at install
time from the installer's own `RELEASE` file, and it does not move afterwards.
This is the number that answers the two questions an operator with ten or a
hundred nodes actually asks: *what is installed on that machine*, and *is it
behind the latest published release*. Self-update reads this one.

A node with no such file predates the stamp, or was hand-patched rather than
installed. reference-node is in that state today. The tool then says `unknown` and must
never substitute the other number.

**The node counter** -- `node_version` in `/var/lib/smart-node/node-release.lock`

How many times *this* node's composition has changed since it was installed. A
fresh node reads 1.0.0 because it has changed zero times; that is correct, not a
defect. It is **not** the node's identity and is not comparable between nodes:
two nodes from the same release both say 1.0.0 while running different things,
and a much-updated node says 1.4.0 without that meaning anything to anyone else.
Earlier docs called this counter "what a cloned node reports". That description was wrong: the counter measures changes to this node only.

## The tool

`sn-version` (python3, stdlib only). Reads what ACTUALLY runs on the node --
`git describe` for source components, the snapshot's own `VERSION` file where
there is no git, the running image tag for image components -- builds a
manifest, and hashes it into a fingerprint.

    sn-version show     the canon release, then the live composition + fingerprint
    sn-version check    canon release, node counter, and what moved since the lock
    sn-version record   what an installer calls: pin the first composition, or
                        record a change to it. Use this one.
    sn-version init     writes the FIRST lock only; refuses to overwrite
    sn-version bump     record a change to an existing lock

On a real change it rewrites the lock, appends to `history`, and emits
`node.composition.changed` into `sn_core.events`, so any agent can ask
"did the system change since X?".

Installed at `/opt/smart-node/bin/sn-version`.

## Three distinctions the output makes, and why each exists

**A change to the node vs a change to the reader.** `?` in a lock does not mean
the component had no version; it means the sn-version that wrote that lock could
not read one. When a newer reader can, the recorded value moves while the
composition does not. Counting that as a change writes a fabricated entry into
the history and moves a number that tags and events key off. `check` and `bump`
both split them; only real changes move the counter or produce a non-zero exit.

**Not installed vs unreadable.** A component genuinely absent from this node --
`mail`, on every node installed without the mail companion -- reports
`not installed`. `?` is reserved for "I could not read a version", which now
means docker itself was unreachable.

**No version of its own.** Four vendored trees have neither git nor a VERSION
file: `admin-runner`, `communication`, `flows`, `/srv/tools`. `show` names them
rather than leaving four bare question marks. Assigning them a reliable version remains open: every obvious source -- a hand-maintained VERSION
file, the installer stage's own version, the node's release -- puts in a number
that is wrong in a different way. Check what each fallback actually measures before assigning a version.

## Wiring -- done, do not redo

All of it is live in `orchestrator/install-on.sh` Phase 3.5: the tool is laid
down, `capabilities.md` is baked into `/etc/smart-node/capabilities.md`, the
canon release is stamped, and `record` runs. It runs at the end of a first
install and of an update alike, which is why it is `record` and not `init`:
`init` overwrote the lock on every re-run, resetting the counter and destroying
the change history, so `check` could only ever answer UNCHANGED.

`CORE_API` is overridable with `SN_CORE_API`; the default is the standard docker
bridge gateway. `MANUAL = {"rein": ..., "postgres": ...}` are versions not
auto-read yet; confirm them per release.

## Do NOT ship

`node-release.lock` is per-node runtime state, produced on each node. Never
commit one into the build.

## Provenance

Extracted from reference-node `/opt/smart-node/bin/sn-version` on 2026-07-24, then wired
into the installer and corrected here on 2026-08-15. Companion doc:
`smart-node-docs system/capabilities.md`, the release-bound capability catalogue
this versioning anchors.
