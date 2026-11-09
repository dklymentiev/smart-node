# Giving a node a real domain

A node installs with whatever hostname you passed as `--node-fqdn` to the installer.
On a fresh install without `--node-fqdn`, the installer falls back to `<name>.sn.vpn`
with a warning -- that scheme is a placeholder, not a real domain, and should be
replaced before use. On an update (`--force` without `--node-fqdn`), the existing
hostname is preserved and no rename happens. Everything the node serves hangs off
that name: `auth.<domain>` (sign-in), `admin.<domain>` (the admin console),
`studio.<domain>` (the Studio), and `line.<domain>` with the voice module. The
older names `panel.<domain>` and `voice.<domain>` answer a
permanent redirect to the new ones, the path and the query kept, so links already
shared keep working; give them DNS records as long as such links are in use.

Putting the node on a domain the outside world can reach is not an installer
operation yet. It is a short manual procedure, written down here because it is
the first thing most operators do after a successful install, and because getting
it partly right is worse than not starting. The node keeps working, and one
action nobody tests breaks quietly.

This should become one command. Until it does, follow all five steps. Skipping
step 4 in particular leaves a node that looks completely healthy.

## Before you start

You need to own the whole zone. Authum holds one cookie domain, so the node
claims `auth.<your-domain>` for itself. On a zone shared with other services that
is a commitment, not a detail.

Use single-label names if you put a CDN in front that issues the edge
certificate. Universal-SSL style certificates cover `example.com` and
`*.example.com`, which is one label. `auth.node1.example.com` is two and gets no
edge certificate. Either keep names one level deep, or serve the record unproxied
so your own reverse proxy presents its certificate.

Decide what is public. The node's root host answers nothing by default, and the
workspace lives on `studio.<domain>` behind Authum. If you want a public page on
the root, that is yours to serve. The node does not ship one.

## The five places

Steps 1 to 4 are inside the node. Step 5 is your DNS and your reverse proxy, and
cannot be automated, because it is your registrar and your edge.

### 1. Authum's environment

`AUTHUM_DOMAIN`, `AUTHUM_RP_ID`, `AUTHUM_AUTH_URL`, `AUTHUM_DEFAULT_REDIRECT`.
These are environment variables, so the container has to be recreated, not
restarted.

`AUTHUM_RP_ID` is the WebAuthn relying party. Changing it invalidates every
registered passkey, because a passkey is bound to it. Set it once, before anyone
registers a credential, or accept that everyone re-registers.

### 2. Authum's route

The node must answer on the public host as well as the internal one, so it stays
reachable over the VPN if the public path breaks.

**Put it in a dynamic file. Never on the container's Traefik label.**

```yaml
# /etc/smart-node/traefik/dynamic/90-public-<domain>.yml
http:
  routers:
    pub-auth:
      # THE DOOR: the one public name with no auth middleware on it.
      rule: "Host(`auth.<public-domain>`)"
      entryPoints: ["websecure"]
      service: "pub-auth"
      tls: {}
  services:
    pub-auth:
      loadBalancer:
        servers: [{url: "http://smart-node-authum:80"}]
        passHostHeader: true
```

The label is the tempting place, because the rule you can already see
(`traefik.http.routers.authum.rule`) is a label, and adding `|| Host(...)` to it
works immediately. It works until the next node update, and then it stops
without a word: **the installer owns that container and recreates it**, and the
rule comes back carrying only the internal name. A dynamic file survives a
container recreation; a label does not.

This is not hypothetical. On 2026-08-14 an update of a live node did exactly
that: `auth.<domain>` began answering 404 while every other public name kept
redirecting to it, so sign-in was down -- and the node's own validation reported
189/189 passed throughout, because it checks internal names.

Every other public name goes in the same file and DOES carry the auth
middleware (`middlewares: ["smart-node-auth@docker"]`). Only `auth.` is bare.

### 3. The identity contract

`/etc/smart-node/identity.env` carries `SMART_NODE_AUTH_URL`. It is written once
at install and nothing revises it afterwards.

Leave `SMART_NODE_DASHBOARD_URL` on the internal name unless you actually intend
to publish the proxy dashboard. This is not a find-and-replace over every
occurrence of the old name. Decide per surface.

### 4. Every consumer that baked the auth URL at install time

This is the step that gets missed, and the reason this document exists.

Editing `identity.env` does not reach components that copied the value when they
were installed. The Studio keeps its own copy in a systemd drop-in:

```
/etc/systemd/system/smart-node-studio.service.d/auth.conf
```

Fix it, run `systemctl daemon-reload`, restart the unit. Search the node for the
old hostname before you declare this done:

```sh
grep -rl '<node>.sn.vpn' /etc/systemd/system/ /etc/smart-node/ /opt/smart-node/*/.env 2>/dev/null
```

Everything else can be right while this one is wrong. Login works, the app works,
passkeys work, and then Logout sends the operator to a hostname that does not
resolve outside the VPN. The failure surfaces in the one action nobody tests
after a migration, and it reads as a broken button rather than a stale variable.

### 5. DNS and the front door

Point the names at your edge, and route them to the node. If the node presents a
self-signed certificate inwards, which is the default, your reverse proxy
terminates TLS and talks to it over the private address, skipping verification
for that hop.

Route at least the workspace and the auth host, and configure the two
identically. A workspace reachable without its auth host is a workspace nobody
can log into.

## Verify, including the part that is easy to skip

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://auth.<domain>/       # 200
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
     https://studio.<domain>/                                        # 302 -> auth, rd back to studio
```

Then, in a browser, and this is the actual acceptance:

1. Sign in.
2. Press Logout, and confirm where it takes you. If it lands on an internal
   hostname, step 4 is unfinished.
3. Sign in again and reach the credential page at `/?page=settings`. That proves
   the auth host is fully yours, not just its login form.

**And run the two curls again after every node update.** This is the check
people skip, and it is the one that catches the failure that actually happens:
an update recreates containers, and anything a public name depended on that the
installer owns comes back the installer's way. The node's own `validate.sh`
cannot cover you here -- it checks the internal `*.sn.vpn` names and will report
a clean pass while a public door is shut.

`tools/public-names.sh` does both curls for every public name it finds on the
node, so this is one command rather than a list you have to keep.

## Rolling back

Keep the previous reverse-proxy configuration until the checks above pass. The
rollback is a one-line edit at your edge, pointing the names back, as long as you
have not deleted the old target. Nothing inside the node needs to be undone for
the outside world to stop using it.
