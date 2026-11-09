# Smart Node build/

Scripts that produce distributable artifacts (LXD images, registry pushes).
Run on a build host with LXD + Docker. Not invoked by the per-install
pipeline.

## build-base-image.sh

Builds `smart-node-base:<version>` — the LXD image with the entire
system layer pre-installed (apt packages, postgres-16 + pgvector,
docker-ce, sops, age, wireguard, fail2ban, ufw, python toolchain,
node-exporter, `security.nesting=true`).

Companion Docker images are NOT baked into this image. No companion is
built into an image from this repository any more; the companions install
natively from the vendored source, and the few containers a node runs
pull their images at install time. This keeps the base ~1.5-2 GB instead of
6 GB and avoids the prior LXD-tarball disaster (slow publish, slow
unpack).

### Build

```bash
./build-base-image.sh --version 0.1.0
```

After build:

```bash
lxc launch smart-node-base/0.1.0 sn-customer-01
# IMPORTANT: lxc publish does NOT carry security.nesting from the build
# container into the published image. Every new container launched from
# this image must opt back in manually before running Smart Node:
lxc config set sn-customer-01 security.nesting true
lxc config set sn-customer-01 limits.memory 4GB
lxc restart sn-customer-01

# Then run install-on.sh against the new container.
```

`provision-lxd-local.sh` already sets `security.nesting=true` for new
containers it creates; if you launch from `smart-node-base` outside
that path, do it yourself.

### Export tarball for distribution

```bash
./build-base-image.sh --version 0.1.0 --export /tmp/dist
# -> /tmp/dist/smart-node-base-0.1.0.tar.gz
```

### Sync with test-node test loop

The `SYSTEM_LAYER_SCRIPT` inlined in `build-base-image.sh` MUST stay
byte-equivalent to whatever produces the test LXC (`test-node`) used for
fast install iteration. If you change one, change the other. Drift
here means the published image and the test loop diverge.

## What this enables

End-to-end customer install times:

| Path | Time |
|---|---|
| Fresh debian-12 + install-on.sh from scratch | ~43 min |
| smart-node-base + install-on.sh | **~2-3 min** |

Validated tactically on `test-node` (Smart Node company-internal test
LXC) on 2026-05-27: with system layer + Docker images cached,
B01-B06 ran in 1m21s end-to-end (vs ~26 min from scratch).
